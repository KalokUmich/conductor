# Conductor

**AI-native collaborative coding inside VS Code.**

Turn engineering discussions into structured decisions and executable code tasks.

Conductor combines real-time team collaboration, isolated Git workspaces, agentic code intelligence, and multi-provider AI into a single developer environment.

[English](#english) | [中文](#中文)

---

<a name="english"></a>

## Why Conductor

Modern AI coding tools are powerful — but they are mostly **single-user tools**.

Tools like GitHub Copilot, Cursor, and ChatGPT help individuals write code. But software development is a **team activity**.

Most engineering knowledge lives in meetings, chat discussions, and design reviews. By the time code is written, the reasoning behind decisions is often lost.

Conductor explores a different approach: instead of starting from code prompts, we start from **engineering discussions**.

## The Idea

Conductor transforms engineering conversations into structured inputs for AI systems.

```
Team Discussion
      ↓
AI Distillation
      ↓
Structured Engineering Decisions
      ↓
Code Intelligence Agent
      ↓
Implementation
```

This allows AI systems to understand not only the codebase, but also the **context behind engineering decisions**.

## What Conductor Provides

### Collaborative Coding Rooms

Teams collaborate inside shared rooms with real-time chat, file sharing, code snippets, and TODO tracking.

### Isolated Git Workspaces

Each collaboration room runs inside its own Git workspace using bare repositories, Git worktrees, and a custom VS Code filesystem (`conductor://`). This allows AI agents to explore code safely without affecting developers' local repositories.

### Agentic Code Intelligence

Conductor uses a tool-based agent loop instead of simple RAG. The agent iteratively navigates the repository using 24 code tools (up to 25 iterations, 500K token budget):

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
| `git_log` | Recent commits; `search=` param filters by commit message |
| `git_diff` | Diff between refs |
| `ast_search` | Structural AST search (ast-grep, `$VAR`/`$$$MULTI` patterns) |
| `get_callees` | Functions called within a function |
| `get_callers` | Functions that call a given function (cross-file) |
| `git_blame` | Per-line authorship with commit hash, author, date |
| `git_show` | Full commit details (message + diff); reads pre-change file at `HEAD~1:path` |
| `find_tests` | Test functions covering a given function/class |
| `test_outline` | Test file structure with mocks, assertions, fixtures |
| `trace_variable` | Data flow tracing: alias detection, arg→param mapping, sink/source patterns |
| `compressed_view` | File signatures + call relationships + side effects (~80% token savings) |
| `module_summary` | Module-level summary: services, models, functions, file list (~95% savings) |
| `expand_symbol` | Expand a symbol from compressed view to full source code |
| `run_test` | Execute a test file or function; returns pass/fail + output (optional verification) |

The agent dynamically selects 8–12 tools per query type (reducing hallucinated calls and token waste). A **Token Budget Controller** emits `NORMAL → WARN_CONVERGE → FORCE_CONCLUDE` signals. An **Evidence Evaluator** gates answers before finalising: requires file:line references, ≥2 tool calls, ≥1 file accessed.

### Multi-Provider AI

Conductor supports AWS Bedrock, Anthropic, and OpenAI. `ProviderResolver` health-checks all configured providers at startup and automatically selects the best available model. All three providers implement `chat_with_tools()`.

## Quick Demo

```bash
# Start the backend
cd backend
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Open the VS Code extension and start a session. Then ask questions like:

- "Where is the loan approval logic implemented?"
- "Trace how the payment service is called."
- "Explain the dependency graph of this module."

## Architecture

```
┌──────────────────────────────────┐     ┌──────────────────────────────────────────┐
│   VS Code Extension              │     │   FastAPI Backend                        │
│                                  │     │                                          │
│  ┌────────────────────────────┐  │ WS  │  ┌───────────────────────────────────┐  │
│  │ SessionFSM                 │  │◄────┼──│ WebSocket Manager (rooms/broadcast)│  │
│  │ WebSocketService           │  │     │  └───────────────────────────────────┘  │
│  │ CollabPanel + @AI commands │  │     │                                          │
│  │ /ask, /pr slash menu       │  │     │  ┌───────────────────────────────────┐  │
│  └────────────────────────────┘  │     │  │ WorkflowEngine                    │  │
│                                  │     │  │  ClassifierEngine (risk/keyword)  │  │
│  ┌────────────────────────────┐  │HTTP │  │  first_match / parallel routes    │  │
│  │ WorkspaceClient            │◄─┼─────┼──│  AgentLoopService (explorers)     │  │
│  │ WorkspacePanel (wizard)    │  │     │  │  provider.call_model() (judges)   │  │
│  │ FileSystemProvider         │  │     │  │  Langfuse @observe decorators     │  │
│  └────────────────────────────┘  │     │  └───────────────────────────────────┘  │
│                                  │     │                                          │
│  ┌────────────────────────────┐  │     │  ┌───────────────────────────────────┐  │
│  │ WorkflowPanel              │  │     │  │ Agent Loop Service                 │  │
│  │ SVG graph visualization    │  │     │  │  QueryClassifier → 3-layer prompt │  │
│  │ agent detail sidebar       │  │     │  │  LLM ←→ 24 Code Tools (dynamic   │  │
│  └────────────────────────────┘  │     │  │  subset) → BudgetController       │  │
│                                  │     │  │  → EvidenceEvaluator → SSE stream │  │
└──────────────────────────────────┘     │  └───────────────────────────────────┘  │
                                         │                                          │
                                         │  ┌───────────────────────────────────┐  │
                                         │  │ AI Provider Layer                  │  │
                                         │  │  ProviderResolver → health check  │  │
                                         │  │  ├─ ClaudeBedrockProvider         │  │
                                         │  │  ├─ ClaudeDirectProvider          │  │
                                         │  │  └─ OpenAIProvider                │  │
                                         │  └───────────────────────────────────┘  │
                                         │                                          │
                                         │  ┌───────────────────────────────────┐  │
                                         │  │ Git Workspace Service              │  │
                                         │  │  bare clone → worktree per room   │  │
                                         │  └───────────────────────────────────┘  │
                                         │                                          │
                                         │  ┌───────────────────────────────────┐  │
                                         │  │ DuckDB Storage                    │  │
                                         │  │  audit_logs / todos / file meta   │  │
                                         │  └───────────────────────────────────┘  │
                                         └──────────────────────────────────────────┘
```

## Project Status

Current prototype includes:

- VS Code collaboration extension with slash-command `@AI` chat and workflow visualization
- FastAPI backend with config-driven multi-agent workflow engine
- Agentic code intelligence (24 tools)
- Multi-agent PR review pipeline (6 specialized agents, parallel dispatch, arbitration, synthesis)
- Isolated Git workspaces per room
- Multi-provider AI support (Bedrock, Anthropic, OpenAI)
- Langfuse self-hosted observability (nested execution trees, cost tracking)
- 1200+ automated tests

## Roadmap

Upcoming features:

- AI decision distillation from discussions
- Code change proposals with diff preview and review
- Jira task generation from engineering decisions
- Model B delegate authentication
- Enterprise access control and audit export
- Persistent codebase memory (background file-summary indexer)

See [ROADMAP.md](ROADMAP.md) for full details.

## Running Tests

```bash
cd backend
pytest                                        # all tests (1200+)
pytest tests/test_code_tools.py -v            # 24 code tools (98 tests)
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

## Contributing

We welcome contributors interested in:

- AI developer tools
- Collaborative coding environments
- Agentic code intelligence

## Documentation

- [Backend Guide](docs/GUIDE.md) — code walkthrough (EN + 中文)
- [Roadmap](ROADMAP.md) — project phases and ADRs
- [Claude](CLAUDE.md) — guide for AI coding assistants

---

<a name="中文"></a>

## 为什么做 Conductor

现代 AI 编程工具很强大——但大多数都是**单人工具**。

GitHub Copilot、Cursor、ChatGPT 帮助个人写代码。但软件开发本质上是**团队活动**。

大多数工程知识存在于会议、聊天讨论和设计评审中。等到代码写出来，决策背后的原因往往已经消失了。

Conductor 探索一种不同的方式：不从代码提示出发，而从**工程讨论**出发。

## 核心思想

Conductor 将工程对话转化为 AI 系统的结构化输入。

```
团队讨论
  ↓
AI 提炼
  ↓
结构化工程决策
  ↓
代码智能 Agent
  ↓
代码实现
```

这让 AI 系统不仅理解代码库，还能理解**工程决策背后的上下文**。

## 功能特性

### 协作编码房间

团队在共享房间内协作，支持实时聊天、文件共享、代码片段和 TODO 追踪。

### 独立 Git 工作区

每个协作房间运行在独立的 Git 工作区中，使用裸仓库、Git worktree 和自定义 VS Code 文件系统（`conductor://`）。AI Agent 可以安全探索代码，不影响开发者本地仓库。

### Agentic 代码智能

Conductor 使用基于工具的 Agent 循环，而非简单的 RAG。Agent 通过 24 个代码工具迭代探索代码库（最多 25 轮迭代，50 万 token 预算）。

工具详情见上方英文部分。

Agent 每种查询类型动态选择 8-12 个工具。**Token 预算控制器**发出 `NORMAL → WARN_CONVERGE → FORCE_CONCLUDE` 信号。**证据评估器**在最终确认答案前把关：要求文件:行号引用、≥2 次工具调用、≥1 个已访问文件。

### 多提供商 AI

支持 AWS Bedrock、Anthropic 和 OpenAI。`ProviderResolver` 在启动时对所有已配置的提供商做健康检查，自动选择最优模型。三个提供商均实现 `chat_with_tools()`。

## 快速开始

```bash
# 启动后端
cd backend
pip install -r requirements.txt
uvicorn app.main:app --reload

# 启动扩展
cd extension
npm install
npm run compile
# 在 VS Code 中按 F5 启动扩展开发主机
```

打开 VS Code 扩展并开始会话，然后提问例如：

- "贷款审批逻辑在哪里实现的？"
- "追踪支付服务是如何被调用的。"
- "解释这个模块的依赖图。"

## 架构

架构图见上方英文部分。

## 项目状态

当前原型包括：

- VS Code 协作扩展（斜杠命令 `@AI` 聊天与工作流可视化面板）
- FastAPI 后端（配置驱动的多 Agent 工作流引擎）
- Agentic 代码智能（24 个工具）
- 多 Agent PR 代码评审（6 个专用 Agent，并行派发，仲裁，综合输出）
- 每个房间独立的 Git 工作区
- 多提供商 AI 支持（Bedrock、Anthropic、OpenAI）
- Langfuse 自托管可观测性（嵌套执行树、成本追踪）
- 1200+ 自动化测试

## Roadmap

即将推出的功能：

- 从讨论中 AI 提炼工程决策
- 代码变更提案与 diff 预览审查
- 从工程决策生成 Jira 任务
- Model B 委托认证
- 企业级访问控制与审计导出
- 持久化代码库记忆（后台文件摘要索引）

详见 [ROADMAP.md](ROADMAP.md)。

## 运行测试

```bash
cd backend
pytest                          # 所有测试 (1200+)
pytest --cov=. --cov-report=html  # 覆盖率报告
```

## 配置

在 `config/conductor.secrets.yaml` 中配置 AI 提供商凭证（参考 `config/conductor.secrets.yaml.example`）。

非敏感配置在 `config/conductor.settings.yaml` 中。

## 参与贡献

欢迎对以下方向感兴趣的贡献者：

- AI 开发者工具
- 协作编码环境
- Agentic 代码智能
