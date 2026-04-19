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

Conductor uses a Brain orchestrator with tool-based agent loops instead of simple RAG. The Brain (strong model) dispatches specialist sub-agents, each navigating the repository using 42 code tools (up to 40 iterations, 500K token budget):

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

The Brain dispatches agents via `dispatch_agent` / `dispatch_swarm` tools, each with per-agent tool sets. A **Token Budget Controller** emits `NORMAL → WARN_CONVERGE → FORCE_CONCLUDE` signals. An **Evidence Evaluator** gates answers before finalising: requires file:line references, ≥2 tool calls, ≥1 file accessed.

### Multi-Provider AI

Conductor supports AWS Bedrock (Claude, Qwen, DeepSeek, Mistral, Nova, NVIDIA, GLM), Anthropic Direct, OpenAI, Alibaba DashScope, and Moonshot. `ProviderResolver` health-checks all configured providers at startup and selects the best available model. All providers implement `chat_with_tools()`.

### Jira Integration + Task Board

Full Jira integration (OAuth 3LO) with 5 agent tools and a 3-phase workflow: **investigate** (code analysis) → **mark code** (TODO markers with dependencies) → **update ticket**. The Task Board shows Jira tickets grouped by Epic (mine=green, unassigned=orange) with dependency-aware drag-and-drop to AI Working Space.

### Cloud Deployment

Docker images ship with dev-default secrets. For ECS/K8s, `CONDUCTOR_*` environment variables override any secret in `conductor.secrets.yaml`. See `docs/GUIDE.md` §21.7 for the full variable reference.

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
│  └────────────────────────────┘  │     │  │ Brain Orchestrator (strong)        │  │
│                                  │     │  │  dispatch_agent / dispatch_swarm  │  │
│  ┌────────────────────────────┐  │HTTP │  │  transfer_to_brain (PR Brain)     │  │
│  │ WorkspaceClient            │◄─┼─────┼──│  ask_user (mid-loop clarify)      │  │
│  │ WorkspacePanel (wizard)    │  │     │  │  Langfuse @observe decorators     │  │
│  │ FileSystemProvider         │  │     │  └───────────────────────────────────┘  │
│  └────────────────────────────┘  │     │                                          │
│                                  │     │  ┌───────────────────────────────────┐  │
│                                  │     │  │ AgentLoopService (sub-agents)     │  │
│                                  │     │  │  4-layer system prompt            │  │
│                                  │     │  │  LLM ←→ 42 Code Tools             │  │
│                                  │     │  │  → BudgetController               │  │
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
                                         │  │ PostgreSQL (Liquibase-managed)    │  │
                                         │  │  6 tables + Langfuse DB           │  │
                                         │  └───────────────────────────────────┘  │
                                         └──────────────────────────────────────────┘
```

## Project Status

Current prototype includes:

- VS Code collaboration extension with slash-command `@AI` chat and workflow visualization
- FastAPI backend with Brain orchestrator (dispatches specialist agents)
- Agentic code intelligence (43 tools, 4-layer prompt architecture)
- Multi-agent PR review pipeline (6 specialized agents, adversarial arbitration, synthesis)
- **Fact Vault** (short-term memory per PR review — task-scoped SQLite cache shared across sub-agents; Phase 9.15)
- **Hardened tree-sitter scan** — subprocess-isolated parsing with SIGKILL-on-timeout + JSX-depth heuristic; tree-sitter upgraded to 0.25 / language-pack (Phase 9.18)
- Isolated Git workspaces per room
- **Task Board**: TODO dependency markers (`{jira:TICKET#N|after:M|blocked:OTHER}`), Epic-grouped Jira tickets, drag-and-drop AI Working Space
- **Chat persistence**: write-through micro-batch Postgres + Redis hot cache
- **Browser tools**: Playwright Chromium automation for web browsing from agents
- Multi-provider AI support (Bedrock, Anthropic, OpenAI, DashScope, Moonshot)
- Langfuse self-hosted observability (nested execution trees, cost tracking)
- Jira integration (OAuth 3LO, 5 agent tools, 3-phase investigate→mark→update workflow)
- Cloud-ready: `CONDUCTOR_*` env vars override secrets for ECS/K8s deployment
- 1777+ automated tests (533 tool-related + parity)

## Roadmap

Upcoming features:

- AI decision distillation from discussions
- Code change proposals with diff preview and review
- Model B delegate authentication (no PAT required)
- Enterprise access control and audit export
- Persistent codebase memory (background file-summary indexer)
- Teams and Slack integrations

See [ROADMAP.md](ROADMAP.md) for full details.

## Running Tests

```bash
cd backend
pytest                                        # all tests (1655+)
pytest tests/test_code_tools.py -v            # code tools (139 tests)
pytest tests/test_agent_loop.py -v            # agent loop + 4-layer prompt (55 tests)
pytest tests/test_brain.py -v                 # Brain orchestrator (64 tests)
pytest tests/test_jira_tools.py -v            # Jira agent tools (21 tests)
pytest tests/test_ai_provider.py -v           # AI providers (131 tests)
pytest tests/test_compressed_tools.py -v      # compressed view tools (24 tests)
pytest tests/test_code_review.py -v           # code review pipeline (67 tests)
pytest --cov=. --cov-report=html              # coverage report

# Tool parity (Python ↔ TypeScript)
make test-parity                              # contract + shape + subprocess validation
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

Conductor 使用 Brain 编排器和基于工具的 Agent 循环，而非简单的 RAG。Brain（强模型）分发专业子 Agent，每个 Agent 通过 42 个代码工具迭代探索代码库（最多 40 轮迭代，50 万 token 预算）。

工具详情见上方英文部分。

Brain 通过 `dispatch_agent` / `dispatch_swarm` 分发 Agent，每个 Agent 配有专属工具集。**Token 预算控制器**发出 `NORMAL → WARN_CONVERGE → FORCE_CONCLUDE` 信号。**证据评估器**在最终确认答案前把关：要求文件:行号引用、≥2 次工具调用、≥1 个已访问文件。

### 多提供商 AI

支持 AWS Bedrock（Claude、Qwen、DeepSeek、Mistral、Nova 等）、Anthropic Direct、OpenAI、阿里 DashScope 和 Moonshot。`ProviderResolver` 在启动时对所有已配置的提供商做健康检查，自动选择最优模型。所有提供商均实现 `chat_with_tools()`。

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
- FastAPI 后端（Brain 编排器分发专业 Agent）
- Agentic 代码智能（43 个工具，4 层 prompt 架构）
- 多 Agent PR 代码评审（6 个专用 Agent，对抗仲裁，综合输出）
- **Fact Vault**（PR review 会话级短期记忆 —— 任务作用域 SQLite 缓存，跨 sub-agent 共享；Phase 9.15）
- **硬化的 tree-sitter 扫描** —— 子进程隔离解析 + SIGKILL 超时 + JSX 嵌套深度启发式；tree-sitter 升级到 0.25 + language-pack（Phase 9.18）
- 每个房间独立的 Git 工作区
- **任务面板**：TODO 依赖标记（`{jira:TICKET#N|after:M|blocked:OTHER}`）、Epic 分组 Jira 票、拖拽 AI 工作区
- **聊天持久化**：写穿透 micro-batch Postgres + Redis 热缓存
- **浏览器工具**：Playwright Chromium 自动化
- 多提供商 AI 支持（Bedrock、Anthropic、OpenAI、DashScope、Moonshot）
- Langfuse 自托管可观测性（嵌套执行树、成本追踪）
- Jira 集成（OAuth 3LO，5 个 Agent 工具，3 阶段 investigate→mark→update 流程）
- 云部署就绪：`CONDUCTOR_*` 环境变量覆盖 ECS/K8s 部署的 secrets
- 1777+ 自动化测试（533 工具相关 + parity）

## Roadmap

即将推出的功能：

- 从讨论中 AI 提炼工程决策
- 代码变更提案与 diff 预览审查
- Model B 委托认证（无需 PAT）
- 企业级访问控制与审计导出
- 持久化代码库记忆（后台文件摘要索引）
- Teams 和 Slack 集成

详见 [ROADMAP.md](ROADMAP.md)。

## 运行测试

```bash
cd backend
pytest                          # 所有测试 (1655+)
pytest --cov=. --cov-report=html  # 覆盖率报告

# 工具一致性验证（Python ↔ TypeScript）
make test-parity
```

## 配置

在 `config/conductor.secrets.yaml` 中配置 AI 提供商凭证（参考 `config/conductor.secrets.yaml.example`）。

非敏感配置在 `config/conductor.settings.yaml` 中。

云部署时，通过 `CONDUCTOR_*` 环境变量覆盖 secrets.yaml 中的值。详见 `docs/GUIDE.md` §21.7。

## 参与贡献

欢迎对以下方向感兴趣的贡献者：

- AI 开发者工具
- 协作编码环境
- Agentic 代码智能
