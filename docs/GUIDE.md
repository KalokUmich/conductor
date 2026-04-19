# Conductor 工程师上手指南

> **面向新加入团队的工程师。** 本指南会先给你建立整体认知，再深入每个子系统的代码细节。建议按顺序阅读第 1-3 节，之后按需查阅后续章节。
>
> **For engineers new to the project.** This guide builds your mental model first, then dives into code. Read sections 1-3 in order; look up the rest as needed.

---

## 目录 / Table of Contents

1. [系统全景 — 先读这里](#1-系统全景--先读这里)
2. [快速启动](#2-快速启动)
3. [端到端请求追踪 — 代码如何流动](#3-端到端请求追踪--代码如何流动)
4. [项目结构](#4-项目结构)
5. [入口文件 main.py](#5-入口文件-mainpy)
6. [配置驱动的工作流引擎](#6-配置驱动的工作流引擎)
7. [Agentic 代码智能 — Agent Loop](#7-agentic-代码智能--agent-loop)
8. [AI 提供商层](#8-ai-提供商层)
9. [Git 工作区管理](#9-git-工作区管理)
10. [聊天系统](#10-聊天系统)
11. [Extension UI 流程](#11-extension-ui-流程)
12. [文件共享](#12-文件共享)
13. [审计日志与 TODO 管理](#13-审计日志与-todo-管理)
14. [身份认证](#14-身份认证)
15. [Jira 集成](#15-jira-集成)
16. [LangExtract 集成](#16-langextract-集成)
17. [Langfuse 可观测性](#17-langfuse-可观测性)
18. [评估系统 (eval/)](#18-评估系统-eval)
19. [测试规范](#19-测试规范)
20. [常见开发任务](#20-常见开发任务)
21. [部署说明](#21-部署说明)

---

## 1. 系统全景 — 先读这里

在看任何代码之前，先理解这个系统在做什么，以及它的核心设计选择。

### 1.1 产品形态

Conductor 是一个 VS Code 扩展，让团队在共享房间（room）里协同工作，并通过 `@AI` 命令让 AI 来理解和评审代码。

**两个核心用户场景：**

```
场景 A：代码问答
用户在聊天框输入 @AI /ask 这个认证流程是怎么实现的？
      ↓
后端运行 AI Agent，Agent 自主探索代码库（grep、读文件、查调用链...）
      ↓
实时流式返回结果，最终给出有文件引用的详细分析

场景 B：PR 代码评审
用户在聊天框输入 @AI /pr main...feature/auth
      ↓
后端解析 Git diff，并行派发 5 个专用 Agent（安全、正确性、并发、可靠性、测试覆盖）
      ↓
仲裁 Agent 统一严重程度，综合 Agent 生成最终报告

场景 C：Jira 智能操作
用户在聊天框输入 @AI /jira create Fix login bug
      ↓
Brain 调度 issue_tracking Agent（强模型 + 500K budget）
Agent 先分析代码（grep → read_file），再创建 Jira 票
      ↓
ask_user 确认票详情 → 创建 → 返回可点击的 Jira 链接
```

### 1.2 两个关键架构决策

**决策一：用 Agent Loop 代替 RAG 管线**

传统 RAG（向量检索 + 生成）是被动的——把代码切块、嵌入成向量、检索相似片段喂给 LLM。

Conductor 的 Agent Loop 是主动的——LLM 自己决定每一步要查什么，像工程师一样一步步追踪代码：

```
检索到 def authenticate() → 看到调用了 jwt.decode() →
跑 get_callers("authenticate") → 找到所有调用方 →
读取关键文件上下文 → 形成完整答案
```

这解决了 RAG 无法处理的"跨文件追踪"和"需要多步推理"的查询。

**决策二：用 YAML/Markdown 配置代替硬编码 Agent 逻辑**

以前，PR 评审的 Agent 逻辑、路由策略、提示词模板散落在 Python 代码里。现在，所有这些都在 `config/` 目录下的配置文件里：

```
config/
├── workflows/pr_review.yaml        # 工作流：哪些路由、并行还是串行
├── agents/security.md              # 单个 Agent：工具列表、预算、指令
└── prompts/review_base.md          # 共享提示词模板
```

工作流引擎（`workflow/engine.py`）读取这些配置并动态编排 Agent，不需要改 Python 代码就能调整 Agent 行为。

### 1.3 主要子系统一览

```
VS Code Extension
├── webview-ui/src/            — React 18 WebView 源代码（esbuild → media/webview.js）
│   ├── components/            — MessageBubble、ChatInput、ChatHeader、TaskBoard、Modals
│   ├── contexts/              — ChatContext、SessionContext、VSCodeContext
│   ├── hooks/                 — useWebSocket、useReadReceipts、useHistoryPagination
│   └── types/                 — postMessage 命令契约（commands.ts）
├── media/webview.js           — React WebView 编译产物（268KB）
└── services/
    ├── localToolDispatcher.ts — 三级工具派发：子进程 → AST → 原生 TS
    ├── astToolRunner.ts       — 6 个 AST 工具（基于 web-tree-sitter）
    ├── treeSitterService.ts   — web-tree-sitter WASM 封装（8 种语言）
    ├── complexToolRunner.ts   — 6 个复杂工具（compressed_view、trace_variable 等）
    └── chatLocalStore.ts      — 本地消息缓存（VS Code globalState）

FastAPI Backend
├── workflow/          — 配置驱动的多 Agent 工作流引擎  ← 核心新增
├── agent_loop/        — LLM Agent Loop（43 个工具）
├── code_review/       — PR 多 Agent 评审管线
├── ai_provider/       — 三提供商抽象层（Bedrock / Anthropic / OpenAI）
├── git_workspace/     — Git 裸仓库 + Worktree 管理
├── chat/              — WebSocket 聊天 + Redis 热缓存 + Postgres 持久化
├── browser/           — Playwright Chromium 浏览工具（browse_url / search_web / screenshot）
├── code_tools/        — 43 个工具实现（代码 + 文件编辑 + Jira + 浏览器 + Fact Vault）+ Python CLI
└── langextract/       — 多厂商 Bedrock 结构化提取集成
```

---

## 2. 快速启动

### 2.1 前置依赖

```bash
# 系统依赖
git --version     # 需要 2.15+（worktree 支持）
rg --version      # ripgrep，code tools 的 grep 工具用它
docker --version  # 运行 Postgres + Redis（数据层）
# ast-grep 可选，用于结构化 AST 搜索（ast_search 工具）
```

> tree-sitter grammar `.wasm` 文件已提交到 `extension/grammars/`，克隆后开箱即用，无需手动下载。如果 ABI 不兼容，重新安装 `web-tree-sitter` npm 包即可，版本已锁定在 `package.json`。

### 2.2 一键安装

```bash
# 创建 Python venv + 安装所有依赖（Python + npm）
make setup
```

等价于：
```bash
python3 -m venv .venv
.venv/bin/pip install -r backend/requirements.txt
cd extension && npm install
```

### 2.3 配置文件

```bash
# 拷贝模板，填入你的 API 密钥
cp config/conductor.secrets.yaml.example config/conductor.secrets.yaml
```

`conductor.secrets.yaml` 最少需要配置以下之一：

```yaml
ai_providers:
  anthropic:
    api_key: "sk-ant-..."   # Anthropic Direct
  # 或者 AWS Bedrock：
  aws_bedrock:
    access_key_id: "..."
    secret_access_key: "..."
    region: "us-east-1"
```

### 2.4 启动数据层（必须先于后端）

```bash
make data-up      # 启动 Postgres（5432）+ Redis（6379）
make db-update    # 应用 Liquibase schema 变更
```

> **为什么必须先启动数据层？** 后端启动时会初始化 `ChatPersistenceService`、`AuditLogService` 等单例，这些服务在构造时会尝试连接 Postgres。如果数据层未就绪，后端启动会报错。

### 2.5 启动后端

```bash
make run-backend  # 开发模式（自动重载，端口 8000）
```

启动日志应该显示：
```
INFO  AI Provider Resolver initialized: active_model=claude-sonnet-4-6, active_provider=anthropic
INFO  Git Workspace module initialized.
INFO  Conductor startup complete.
```

如果看到 `asyncpg.exceptions.ConnectionDoesNotExistError`，说明 Postgres 未就绪，先运行 `make data-up`。

### 2.6 验证

```bash
# 健康检查
curl http://localhost:8000/health
# → {"status": "ok"}

# 代码问答（SSE 流，Brain 编排器）
curl -N -X POST http://localhost:8000/api/context/query/stream \
  -H "Content-Type: application/json" \
  -d '{"room_id": "demo", "query": "how does authentication work?"}'
```

### 2.7 运行测试

```bash
make test-backend  # 全量后端测试（1300+）

# 或者细粒度：
cd backend
pytest tests/test_agent_loop.py -v    # Agent Loop 测试
pytest tests/test_code_tools.py -v    # 代码工具测试
pytest -k "workflow" -v               # 工作流引擎测试
pytest --cov=. --cov-report=html      # 覆盖率报告

# 工具一致性（Python ↔ TypeScript）
make test-parity
```

---

## 3. 端到端请求追踪 — 代码如何流动

这一节追踪两个最重要的用户操作从前端到后端的完整代码路径。**这是理解系统最快的方式。**

### 3.1 场景 A：用户输入 `@AI /ask 认证逻辑在哪里？`

**第一步：Extension 解析命令**（`extension/webview-ui/src/components/chat/ChatInput.tsx`）

用户在 textarea 里输入 `@AI /ask 认证逻辑在哪里？` 并按 Enter。

```typescript
// ChatInput.tsx — handleSend() + slashCommands.ts
const { query, isAI } = parseMessageForAI(text);
    // 匹配 "@AI /ask xxx" 或 "@AI /pr xxx" 或 "@AI /jira xxx"
    const slashMatch = text.match(/@AI\s+\/(\w+)\s+(.*)/is);
    if (slashMatch) {
        const cmd = SLASH_COMMANDS.find(c => c.name === '/' + slashMatch[1]);
        query = cmd ? cmd.transform(slashMatch[2]) : text;
    }
    // "/ask" 直接透传，"/pr" 加 "do PR" 前缀，"/jira" 加 "[jira]" 前缀+意图检测
    vscode.postMessage({ command: 'askAI', query, workspacePath });
}
```

**第二步：Extension Host 转发请求**（`extension/src/extension.ts`）

```typescript
// extension.ts — message handler
case 'askAI':
    // 发起 SSE 请求到后端
    const response = await fetch(`${backendUrl}/api/context/query/stream`, {
        method: 'POST',
        body: JSON.stringify({ query: message.query, workspace_path: message.workspacePath }),
    });
    // 逐行读取 SSE 事件，推送到 WebView 展示实时进度
    for await (const event of readSSE(response)) {
        panel.webview.postMessage({ type: 'agentProgress', event });
    }
```

**第三步：Backend 路由到 Brain 编排器**（`backend/app/agent_loop/router.py`）

```python
@router.post("/api/context/query/stream")
async def context_query_stream(req: ContextQueryRequest, ...):
    # ... 鉴权、解析 worktree、构建 ToolExecutor ...
    engine = WorkflowEngine(provider=agent_provider, explorer_provider=explorer_provider, ...)
    brain_context = {"query": req.query, "workspace_path": str(worktree_path)}

    async def event_generator():
        async for event in engine.run_brain_stream(brain_context):
            yield f"event: {event.kind}\ndata: {json.dumps(event.data)}\n\n"
    return StreamingResponse(event_generator(), media_type="text/event-stream")
```

**第四步：Brain 决定 dispatch 谁**（`backend/app/agent_loop/brain.py`）

Brain（强模型）跑自己的 LLM 循环，可调用 4 个 meta-tool：

- `dispatch_agent("agent_name", query)` — 单 agent 探索
- `dispatch_swarm("preset_name", queries=[...])` — 并行多 agent
- `transfer_to_brain("pr_review", params)` — 一次性切到专精 brain（如 PR review）
- `ask_user(...)` — 中途向用户确认方向

例：用户问"认证逻辑"，Brain 看到这是简单单 agent 任务，调用 `dispatch_agent("entry_point_finder", "认证逻辑入口")`。

**第五步：被 dispatch 的子 agent 跑 AgentLoopService**（`backend/app/agent_loop/service.py`）

```python
async def run_stream(self, query, workspace_path):
    messages = [{"role": "user", "content": query}]
    system_prompt = build_sub_agent_system_prompt(
        agent_name=self._agent_identity["name"],
        agent_description=self._agent_identity["description"],
        agent_instructions=self._agent_identity["instructions"],
        strategy_key=self._forced_strategy or None,
        ...
    )

    for i in range(self.max_iterations):
        response = provider.chat_with_tools(messages, tools=active_tools, system=system_prompt)
        if response.stop_reason == "end_turn":
            if evidence_ok(response.text, tool_calls_made):
                return AgentResult(answer=response.text, ...)
            messages.append(feedback_message("需要具体文件引用"))
            continue
        for tool_call in response.tool_calls:
            result = execute_tool(tool_call.name, workspace_path, tool_call.input)
            messages.append(tool_result_block(tool_call.id, result))
```

**整条链路：**
```
用户输入 → ChatInput.tsx 解析 → useWebSocket/extension.ts SSE 请求 →
agent_loop/router.py → WorkflowEngine.run_brain_stream() →
Brain LLM 循环 → dispatch_agent / dispatch_swarm / transfer_to_brain →
子 agent AgentLoopService.run_stream() → LLM ↔ execute_tool() 循环 →
SSE 事件流回 → ChatContext → ThinkingIndicator/MessageBubble 实时渲染
```

---

### 3.2 场景 B：用户输入 `/pr main...feature/auth`

**第一步：Extension 解析并发送**

`slashCommands.ts` 的 transform 把 `/pr X` 改写成带 marker 的查询：

```typescript
{ name: "/pr", transform: (args) => `${marker(QUERY_TYPE.CODE_REVIEW)} ${args}` }
// "/pr main...feature/auth" → "[query_type:code_review] main...feature/auth"
```

Marker 是给 Brain LLM 看的暗号，约定值集中在 `backend/app/agent_loop/query_markers.py`（`QueryType` enum），前端通过 `QUERY_TYPE` 常量手动同步。

**第二步：Brain 识别 marker，transfer 到 PR Brain**

Brain 在 prompt 中被教过 `[query_type:code_review]` 约定，看到 marker 后调用：

```python
transfer_to_brain("pr_review", params={"workspace_path": ..., "diff_spec": "main...feature/auth"})
```

`brain.py:_transfer_to_brain` 校验白名单（目前只允许 `pr_review`），然后启动 `PRBrainOrchestrator`。

**第三步：PRBrainOrchestrator 6 阶段流水线**（`backend/app/agent_loop/pr_brain.py`）

```
Phase 1: 预计算（parse_diff、classify_risk、prefetch_diffs、impact_graph）
Phase 2: 并行 dispatch review agents（correctness、correctness_b、concurrency、security、reliability、performance、test_coverage）
Phase 3: 后处理（evidence_gate → post_filter → dedup → score_and_rank）
Phase 4: 对抗仲裁（pr_arbitrator 试图反驳每条 finding）
Phase 5: Merge recommendation（确定性）
Phase 6: 综合（Brain 作为最终评判，看到正方证据 + 反方反驳）
```

每个 review agent 的 `.md` frontmatter 都声明 `skill: code_review_pr`，会通过 `forced_skill` 把 `prompts.py` 里的 `INVESTIGATION_SKILLS["code_review_pr"]` 注入 Layer 3。这个 skill 是 PR review 的单一来源：包含 senior engineer persona、provability framework、DO NOT FLAG 列表、PR-introduced 验证规则和 JSON 输出格式。

**整条链路：**
```
"/pr main...feature/auth" →
slashCommands.transform → "[query_type:code_review] main...feature/auth" →
Brain LLM 看到 marker → transfer_to_brain("pr_review") →
PRBrainOrchestrator 6 阶段 →
parallel review agents → arbitration → synthesis →
最终 ReviewResult
```

---

## 4. 项目结构

```
backend/
├── app/
│   ├── main.py                    # App 工厂、lifespan 启动/关闭
│   ├── config.py                  # 从 YAML 读取 Settings + Secrets
│   │
│   ├── workflow/                  # Brain 编排器宿主 + agent/swarm 配置加载
│   │   ├── models.py              # Pydantic 模型：AgentConfig、BrainConfig、SwarmConfig
│   │   ├── loader.py              # 加载 Markdown Agent 文件 + Brain/Swarm YAML
│   │   ├── engine.py              # WorkflowEngine.run_brain_stream() — Brain 入口
│   │   ├── router.py              # /api/brain/swarms — Agent Swarm UI 数据源
│   │   └── observability.py       # Langfuse @observe 装饰器（禁用时零开销）
│   │
│   ├── agent_loop/                # LLM Agent 循环引擎 + Brain 编排器
│   │   ├── service.py             # AgentLoopService — LLM 循环 + 工具派发
│   │   ├── brain.py               # AgentToolExecutor — dispatch_agent / dispatch_swarm / transfer_to_brain
│   │   ├── pr_brain.py            # PRBrainOrchestrator — PR 评审专用确定性管线
│   │   ├── query_markers.py       # QueryType enum + marker 解析（前后端共享约定）
│   │   ├── budget.py              # BudgetController — token 预算三级信号
│   │   ├── trace.py               # SessionTrace — JSON 追踪（离线分析用）
│   │   ├── evidence.py            # EvidenceEvaluator — 答案质量门控
│   │   ├── prompts.py             # 四层 System Prompt 构建 + 9 种 Investigation Skills
│   │   └── router.py              # POST /api/context/query/stream（SSE）
│   │
│   ├── code_tools/                # 43 个工具（代码 + 文件编辑 + Jira + 浏览器 + Fact Vault）
│   │   ├── tools.py               # 所有工具实现 + execute_tool() 调度器
│   │   ├── schemas.py             # Pydantic 模型 + LLM 工具定义（TOOL_DEFINITIONS）
│   │   ├── output_policy.py       # 每工具截断策略（预算自适应）
│   │   ├── __main__.py            # Python CLI 入口：python -m app.code_tools <tool> <ws> '<params>'
│   │   └── router.py              # /api/code-tools/ 直接调用接口
│   │
│   ├── ai_provider/               # LLM 提供商抽象层
│   │   ├── base.py                # AIProvider ABC + ToolCall/ToolUseResponse/TokenUsage
│   │   ├── claude_bedrock.py      # AWS Bedrock Converse API
│   │   ├── claude_direct.py       # Anthropic Messages API
│   │   ├── openai_provider.py     # OpenAI Chat Completions
│   │   └── resolver.py            # ProviderResolver — 健康检查 + 自动选优
│   │
│   ├── code_review/               # 多 Agent PR 评审管线（10 步）
│   │   ├── service.py             # CodeReviewService — 编排评审流程
│   │   ├── agents.py              # 专用评审 Agent（并行派发）
│   │   ├── models.py              # PRContext、ReviewFinding、ReviewResult
│   │   ├── diff_parser.py         # git diff → PRContext
│   │   ├── risk_classifier.py     # 5 维度风险分类
│   │   ├── ranking.py             # 发现结果评分排序
│   │   ├── dedup.py               # 发现结果去重合并
│   │   └── router.py              # /api/code-review/ 接口（含 SSE 流）
│   │
│   ├── git_workspace/             # Git 工作区管理
│   │   ├── service.py             # GitWorkspaceService（裸仓库 + worktree）
│   │   ├── delegate_broker.py     # DelegateBroker（Model B 预留）
│   │   └── router.py              # /api/git-workspace/ 接口
│   │
│   ├── langextract/               # 多厂商 Bedrock 结构化提取
│   │   ├── provider.py            # BedrockLanguageModel — 所有 Bedrock 厂商
│   │   ├── catalog.py             # BedrockCatalog — 动态模型发现
│   │   ├── service.py             # LangExtractService 异步包装
│   │   └── router.py              # GET /api/langextract/models
│   │
│   ├── repo_graph/                # AST 符号图（供 code tools 使用）
│   │   ├── parser.py              # tree-sitter AST + 正则回退
│   │   ├── graph.py               # networkx 依赖图 + PageRank
│   │   └── service.py             # RepoMapService（图构建 + 缓存）
│   │
│   ├── chat/                      # WebSocket 聊天 + 持久化
│   │   ├── manager.py             # ConnectionManager — WebSocket 房间管理
│   │   ├── redis_store.py         # Redis 热缓存（6h TTL）
│   │   ├── persistence.py         # ChatPersistenceService — 写穿透 micro-batch Postgres
│   │   └── router.py              # /ws/chat/{room_id}, /chat/{room_id}/history, DELETE /chat/{room_id}
│   │
│   ├── browser/                   # Playwright 网页浏览工具
│   │   ├── service.py             # BrowserService — Chromium 自动化
│   │   ├── tools.py               # browse_url、search_web、screenshot 实现
│   │   └── router.py              # /api/browser/ 接口
│   │
│   ├── files/                     # 文件上传下载（PostgreSQL 元数据）
│   ├── audit/                     # PostgreSQL 审计日志
│   ├── todos/                     # PostgreSQL TODO 追踪
│   ├── auth/                      # AWS SSO + Google OAuth
│   ├── policy/                    # 自动应用安全评估
│   └── workspace_files/           # Worktree 文件 CRUD
│
├── config/
│   ├── conductor.settings.yaml    # 非敏感设置（已提交）
│   ├── conductor.secrets.yaml     # API 密钥等敏感信息（gitignore）
│   ├── brain.yaml                 # Brain 限制 + core_tools
│   ├── brains/
│   │   └── pr_review.yaml         # PR Brain 配置（review agents、budget weights、post_processing）
│   ├── agents/                    # Agent 定义文件（YAML frontmatter + Markdown 正文）
│   │   ├── security.md            # PR review agent：认证/注入/XSS
│   │   ├── correctness.md         # PR review agent：逻辑/状态/持久化
│   │   └── ... (more)
│   ├── swarms/                    # Swarm 预设（agent 组 + parallel/sequential + synthesis_guide）
│   └── prompts/
│       └── ... 共享提示词模板
│
├── requirements.txt
└── tests/                         # 1300+ 测试
    ├── conftest.py                # 中央 stub（cocoindex、litellm 等）
    ├── test_code_tools.py         # 139 个：42 工具 + 调度器 + 多语言
    ├── test_agent_loop.py         # 55 个：循环 + 四层 Prompt + 完整性检查
    ├── test_budget_controller.py  # 20 个：预算信号
    ├── test_compressed_tools.py   # 24 个：压缩视图工具
    ├── test_evidence.py           # 19 个：证据门控
    ├── test_symbol_role.py        # 24 个：符号角色分类
    ├── test_output_policy.py      # 19 个：截断策略
    ├── test_langextract.py        # 57 个：Bedrock 多厂商
    ├── test_repo_graph.py         # 72 个：AST + 依赖图
    ├── test_chat_persistence.py   # ChatPersistenceService — micro-batch Postgres
    ├── test_browser_tools.py      # 浏览器工具（Playwright，mocked）
    └── ...
```

> **为什么这样分目录？** FastAPI 鼓励将路由（HTTP 层）和服务（业务逻辑层）分离。每个功能模块是一个子包，有自己的 `router.py`（路由）和 `service.py`（业务逻辑），互不耦合。

---

## 5. 入口文件 main.py

`main.py` 的 `lifespan` 函数控制启动和关闭，是理解整个后端初始化流程的入口。

```python
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    settings = load_settings()

    # 1. Git 工作区服务
    git_service = GitWorkspaceService()
    if settings.git_workspace.enabled:
        await git_service.initialize(settings.git_workspace)
    app.state.git_workspace_service = git_service

    # 2. AI 提供商解析器（健康检查所有配置的提供商，选最优）
    conductor_config = get_config()
    resolver = ProviderResolver(conductor_config)
    resolver.resolve()
    set_resolver(resolver)
    app.state.agent_provider      = resolver.get_active_provider()      # 强模型
    app.state.explorer_provider   = resolver.get_explorer_provider()    # 轻量模型（可选）
    app.state.classifier_provider = resolver.get_classifier_provider()  # 分类模型（可选）

    # 3. 单例服务初始化（必须在 lifespan 中用 engine= 参数首次调用）
    TodoService.get_instance(engine=engine)
    AuditLogService.get_instance(engine=engine)
    FileStorageService.get_instance(engine=engine)
    ChatPersistenceService.get_instance(engine=engine)
    # 注意：不在 lifespan 中初始化会导致首次请求时 RuntimeError

    # 4. Langfuse 可观测性（self-hosted，可选）
    init_langfuse(settings)

    # 5. Ngrok 隧道（VS Code Remote-WSL 场景，可选）
    if ngrok_cfg.get("enabled"):
        start_ngrok(port=settings.server.port, ...)

    # 6. Bedrock 模型目录（动态发现可用模型，可选）
    catalog = BedrockCatalog(region=bedrock_region)
    catalog.refresh()
    app.state.bedrock_catalog = catalog

    yield  # ← 应用在这里运行

    # 关闭清理
    stop_ngrok()
    langfuse_flush()
    await git_service.shutdown()
```

**关于 PNA 中间件（Private Network Access）**

Chrome 105+ 会阻止 `vscode-webview://` 来源向 localhost 发请求，除非服务器返回 `Access-Control-Allow-Private-Network: true`。

注意：这里使用了**纯 ASGI 中间件**（不是 `BaseHTTPMiddleware`）。原因是 `BaseHTTPMiddleware` 会缓冲响应 body，会悄无声息地杀死所有 WebSocket 连接（返回 close code 1006）。纯 ASGI 中间件对 HTTP 和 WebSocket 请求都是安全的。

---

## 6. Brain 编排器

Brain 是 Conductor 的核心编排层。它是一个跑在强模型上的 LLM 循环，**自己**决定 dispatch 哪些专精子 agent —— 没有基于关键词的 classifier，没有 YAML 路由表。

### 6.1 核心抽象

```
Query → Brain (LLM 循环) → 选择 meta-tool → 子 agent / swarm / specialized brain → 结果 → Brain 综合
```

Brain 在它的 prompt 里被告知所有可用的子 agent + swarm（来自 `config/agents/*.md` 和 `config/swarms/*.yaml`）。它通过下面 4 个 meta-tool 调度：

| Meta-tool | 用途 |
|---|---|
| `dispatch_agent("name", query)` | 单个专精 agent 探索一个具体目标 |
| `dispatch_swarm("preset", queries=[...])` | 用预设并行跑 3-6 个 agent |
| `transfer_to_brain("pr_review", params)` | 一次性切换到专精 brain（目前只有 PR Brain） |
| `ask_user(question, options)` | 中途向用户确认方向 |

### 6.2 Agent 定义文件解析

每个 agent 是一个 Markdown 文件，YAML frontmatter 是元数据，正文是该 agent 的人格 + 指令：

```markdown
---
name: security
description: 检查认证、注入、密钥泄漏等安全风险
model: explorer          # explorer（轻量模型）或 strong（强模型）

tools:                   # 这个 agent 可用的工具
  - grep
  - read_file
  - find_references
  - get_callers
  - trace_variable       # 数据流追踪，安全 agent 必备

skill: code_review_pr    # 注入 prompts.py 里的 code_review_pr skill（senior engineer review 风格）

limits:
  max_iterations: 20
  budget_tokens: 200000
---

## 安全审查策略

1. 检查认证和授权变更
   - 用 grep 搜索 jwt、token、session 相关代码
   - 用 get_callers 找所有调用认证函数的地方
   ...

2. 检查注入风险
   - 用 trace_variable 追踪用户输入如何流向 SQL 查询
   ...
```

### 6.3 Swarm 预设

`config/swarms/*.yaml` 定义"一组 agent 加并行/顺序模式 + 综合指南"，让 Brain 一次 dispatch 多个 agent：

```yaml
# config/swarms/business_flow.yaml
name: business_flow
description: 跨多角度并行追踪一个业务流程
mode: parallel
agents:
  - explore_implementation
  - explore_usage
  - explore_data_flow
synthesis_guide: |
  按入口 → 主流程 → 数据/状态变更 → 副作用的顺序综合。
  必须给出 file:line 引用。
```

### 6.4 PR Brain — 唯一的专精 brain

`pr_review` 是唯一通过 `transfer_to_brain` 激活的专精 brain。它运行 `PRBrainOrchestrator`，是一个 6 阶段确定性流水线（pre-compute → dispatch review agents → post-process → arbitration → merge recommendation → synthesis），实现细节见 `backend/app/agent_loop/pr_brain.py`。

**PR-review-scoped infrastructure**（Phase 9.15 + 9.18 硬化层）：

* **Fact Vault（短期记忆）** — `backend/app/scratchpad/`。每次 PR review 启动时 `PRBrainOrchestrator` 开一个 SQLite session 文件（`~/.conductor/scratchpad/{task_id}-{uuid}.sqlite`，如 `ado-MyProject-pr-12345-b37b7979.sqlite`），所有 sub-agent 的 `grep` / `read_file` / `find_symbol` 调用经由 `CachedToolExecutor` 透明去重：命中 exact-key 或 range-intersection 超集直接返回缓存，未命中才真正执行。典型 7-agent 并行 review 的 cache hit rate 约 25-40%；session 结束调用 `cleanup()` 删文件 + WAL sidecar。
* **Tree-sitter scan 硬化** — `backend/app/repo_graph/parse_pool.py`。每次文件解析跑在独立子进程（`forkserver` 启动方式，POSIX），超时 60s 后主进程 `SIGKILL` worker 并 respawn。这是唯一可靠的超时原语 —— tree-sitter 的 Python binding 在 C-level parse 中持有 GIL，任何线程级超时都会被卡死（sentry-007 诊断用 py-spy 证实）。配套的 JSX-depth heuristic 预判 `.tsx` > 20KB 且嵌套 ≥15 层的文件，直接走 regex fallback 避开首次 60s SIGKILL 预算。
* **降级信号透出** — 当 tree-sitter 超时/失败时，`FileSymbols.extracted_via = "regex"`；`find_symbol` 给每条匹配加 `extracted_via: "regex"` 标记，`file_outline` 变成 `{"definitions": [...], "extracted_via": "regex", "note": "..."}` dict。agent 通过工具 description 得知 "看到这个标记就用 grep / read_file 取信更高的结果"。

上述基础设施在 Phase 9.15 Fact Vault + 9.18 Scan Hardening 中建成，具体历史见 `ROADMAP.md`。接下来的 PR Brain v2 重构（`docs/PR_BRAIN_V2_PLAN.md`）会在此之上切换到 coordinator pattern —— Brain 自己规划 investigations，Haiku workers 回答窄作用域的 checks，严重度分类统一在 Brain 合成阶段。

### 6.5 Query Markers — 前后端共享约定

前端 slash 指令（`/pr`、`/jira`、`/summary`、`/diff`）会在 query 前面加 `[query_type:X]` marker，作为给 Brain LLM 的 routing 暗号。Marker 字符串集中定义在 `backend/app/agent_loop/query_markers.py` 的 `QueryType` enum，前端的 `extension/webview-ui/src/utils/slashCommands.ts` 用同名 `QUERY_TYPE` 常量手动同步（修改时两边都要改）。

Marker **不被任何 Python 代码 parse** —— 它只是 prompt 上下文里的提示，让 Brain 在意图模糊时也能可靠地选对 dispatch 路径。

### 6.6 Brain Swarms API

```bash
# 返回 Brain 可调度的所有专精 brain + swarm（含 agent 组成）
GET /api/brain/swarms
# → {
#     "brain_model": "claude-sonnet-4-6",
#     "core_tools": [...],
#     "specialized_brains": [{ "name": "pr_review", "type": "brain", "mode": "pipeline", "agents": [...] }],
#     "swarms": [{ "name": "business_flow", "type": "swarm", "mode": "parallel", "agents": [...] }]
#   }
```

供 extension 的 Agent Swarm UI tab 可视化 Brain 的 handoff 目标（`transfer_to_brain` 与 `dispatch_swarm`）。

---

## 7. Agentic 代码智能 — Agent Loop

### 7.1 为什么不用 RAG？

传统 RAG：

```
代码切块 → 向量嵌入 → 相似度检索 → 喂给 LLM → 回答
```

问题：检索是静态的，结果质量取决于向量匹配，无法处理"先找到函数 A，再追踪 A 调用了什么"这类链式推理。

Agent Loop：

```
LLM 看到问题 → 决定先 grep 搜索关键词
              → 看到结果，决定读某个文件
              → 看到函数调用，决定用 get_callers 查调用方
              → 追踪完整调用链，形成答案
```

LLM 每一步都能基于已有信息决定下一步，可以进行真正的多步推理。

### 7.2 43 个工具（代码 + 文件编辑 + Jira + 浏览器 + Fact Vault）

工具分布在三个 Registry 中，通过 `execute_tool(name, workspace, params)` 统一调度：
- **代码工具** (32)：`code_tools/tools.py`（含 Phase 9.15 加入的 `search_facts`）
- **Jira 工具** (5)：`integrations/jira/tools.py`
- **浏览器工具** (6)：`browser/tools.py`

**搜索工具：**

| 工具 | 参数 | 说明 |
|------|------|------|
| `grep` | `pattern`, `path?`, `file_glob?` | Ripgrep 正则搜索，自动排除 .git/node_modules |
| `ast_search` | `pattern`, `lang?`, `path?` | 结构化 AST 搜索（ast-grep），`$VAR` 匹配任意节点 |
| `find_symbol` | `name`, `kind?` | AST 符号定义搜索，结果含角色分类 |
| `find_references` | `name`, `file?` | 符号引用搜索（grep + AST 验证）|

**文件读取工具：**

| 工具 | 参数 | 说明 |
|------|------|------|
| `read_file` | `path`, `start_line?`, `end_line?` | 读文件内容，支持行范围 |
| `list_files` | `path?`, `depth?`, `glob?` | 目录树，支持 glob 过滤 |
| `file_outline` | `path` | 文件中所有定义及行号 |
| `compressed_view` | `path`, `focus?` | 签名+调用关系+副作用，节省 ~80% token |
| `module_summary` | `path` | 模块级摘要：服务/模型/函数列表，节省 ~95% token |
| `expand_symbol` | `name`, `file?` | 从压缩视图还原完整源码 |

**调用图工具：**

| 工具 | 参数 | 说明 |
|------|------|------|
| `get_callers` | `name`, `file?` | 谁调用了这个函数（跨文件）|
| `get_callees` | `name`, `file` | 这个函数调用了什么 |
| `get_dependencies` | `file` | 这个文件导入了哪些文件 |
| `get_dependents` | `file` | 哪些文件导入了这个文件 |

**Git 工具：**

| 工具 | 参数 | 说明 |
|------|------|------|
| `git_log` | `file?`, `search?`, `n?` | 最近提交，支持按文件和提交信息搜索 |
| `git_diff` | `base`, `head?`, `file?` | 两个 ref 之间的 diff |
| `git_blame` | `file`, `start_line`, `end_line` | 每行代码的作者信息 |
| `git_show` | `ref` | 完整 commit 详情，可用 `HEAD~1:file.py` 查看变更前文件 |

**测试工具：**

| 工具 | 参数 | 说明 |
|------|------|------|
| `find_tests` | `name`, `file?` | 找覆盖某函数/类的测试 |
| `test_outline` | `file` | 测试文件结构（mock、断言、fixture）|
| `run_test` | `file`, `function?` | 实际运行测试，返回通过/失败 + 输出 |

**数据流工具：**

| 工具 | 参数 | 说明 |
|------|------|------|
| `trace_variable` | `name`, `file`, `line` | 追踪变量流向：别名检测、参数传递、sink/source |
| `detect_patterns` | `path?`, `patterns?` | 架构模式检测（单例、工厂、观察者等）|

**其他代码工具：**

| 工具 | 参数 | 说明 |
|------|------|------|
| `glob` | `pattern`, `path?` | 快速文件模式匹配（如 `**/*.ts`）|
| `git_diff_files` | `base`, `head?` | 列出两个 ref 之间变更的文件列表 |
| `git_hotspots` | `n?`, `since?` | 最近变更热点文件（变更次数 × 作者数）|
| `list_endpoints` | `path?`, `framework?` | 提取 API 路由定义（Flask/FastAPI/Express 等）|
| `extract_docstrings` | `path` | 提取模块中函数/类的文档字符串 |
| `db_schema` | `path?` | 数据库 Schema 内省（SQLAlchemy 模型）|

**文件编辑工具：**

| 工具 | 参数 | 说明 |
|------|------|------|
| `file_edit` | `path`, `old_text`, `new_text` | 搜索替换编辑（需先 `read_file`，防止覆盖未读内容）|
| `file_write` | `path`, `content` | 完整文件写入/创建（已存在文件需先 `read_file`）|

> ⚠️ **file_write / file_edit 的 content 字段保留原始空白**。`_repair_tool_params` 的 Pattern 3 对所有字符串默认 `.strip()`，但 `file_write.content` / `file_edit.old_string` / `file_edit.new_string` 通过白名单排除 —— 不然会静默吃掉 POSIX 文本文件的 trailing newline（Phase 9.18 step 3 修正）。

**Fact Vault 工具**（Phase 9.15，仅在 PR review session 内可用）：

| 工具 | 参数 | 说明 |
|------|------|------|
| `search_facts` | `tool?`, `path?`, `pattern?`, `limit?` | 查询本次 PR review 已缓存的 tool 调用结果（`grep` / `read_file` / `find_symbol` 等）。返回 metadata only — agent 看到 fact 存在后可决定是直接重跑（由 CachedToolExecutor 透明命中缓存）还是跳过这次查询 |

**Jira 集成工具**（详见 [§15 Jira 集成](#15-jira-集成)）：

| 工具 | 参数 | 说明 |
|------|------|------|
| `jira_search` | `query`, `max_results?` | JQL 或自由文本搜索；快捷方式：`my tickets` / `my sprint` / `blockers` |
| `jira_get_issue` | `issue_key` | 获取 Issue 完整详情（描述、评论、子任务）|
| `jira_create_issue` | `project_key`, `summary`, `description`, ... | 创建 Ticket（ADF 描述、代码块、parent_key 子任务）|
| `jira_update_issue` | `issue_key`, `transition_to?`, `comment?`, ... | 状态转换、评论、字段更新（Done/Closed/Resolved 被阻止）|
| `jira_list_projects` | — | 列出可访问的 Jira 项目 |

**浏览器工具：**

| 工具 | 参数 | 说明 |
|------|------|------|
| `web_search` | `query` | 网页搜索 |
| `web_navigate` | `url` | 无头浏览器导航到 URL |
| `web_click` | `selector` | 点击页面元素 |
| `web_fill` | `selector`, `value` | 填写表单字段 |
| `web_screenshot` | — | 截取页面截图 |
| `web_extract` | `selector?` | 提取页面内容 |

### 7.3 工具输出策略（output_policy.py）

不同工具有不同的截断策略，避免单个工具结果撑爆上下文窗口：

```python
# 搜索工具按结果数量截断
"grep":          Policy(max_results=50)
# 文件读取按行截断（不截断到行中间）
"read_file":     Policy(max_lines=300, truncate_unit="lines")
# Git 工具给更宽松的字符限制
"git_show":      Policy(max_chars=8000)
# 压缩视图工具不需要截断（本身已经是紧凑格式）
"compressed_view": Policy(max_chars=20000)

# 预算自适应：剩余 token < 100K 时，所有限制缩小 50%
if budget_controller.remaining_tokens < 100_000:
    policy.scale(0.5)
```

### 7.4 Token 预算控制器（BudgetController）

```python
# budget.py
class BudgetController:
    def check_and_signal(self, usage: TokenUsage) -> BudgetSignal:
        ratio = self.total_input_tokens / self.max_input_tokens

        if ratio < 0.70:
            return BudgetSignal.NORMAL         # 正常探索
        elif ratio < 0.90:
            return BudgetSignal.WARN_CONVERGE  # 收敛：禁止宽泛搜索，只允许验证调用
        else:
            return BudgetSignal.FORCE_CONCLUDE # 强制结束：LLM 必须立即给出答案
```

当信号变为 `WARN_CONVERGE` 时，agent loop 会在系统提示词中注入约束，阻止 LLM 继续 grep 或 find_symbol 等宽泛搜索。`FORCE_CONCLUDE` 时直接注入"请立即给出最终答案"的指令。

### 7.5 四层系统提示词（prompts.py）

每次 LLM 调用的 system prompt 由四层组成（遵循 Anthropic 官方提示词规范）：

```
L1: Identity（系统提示词，每个 Agent 独有）
    ├── Agent 身份：名称、职责、调查视角
    ├── 目标导向：理解代码行为、定位功能、追踪数据流
    └── 答案格式：必须包含文件:行号引用，代码块引用实际代码

L2: Tools（按查询类型精选工具集）
    ├── brain.yaml core_tools ∪ agent .md 配置的专用工具
    └── 工具描述丰富化：3-4 句，何时用 / 何时不用 / 不返回什么

L3: Skills & Guidelines（Agent 间共享上下文）
    ├── 工作区布局、项目文档（README/CLAUDE.md）
    ├── 调查方法论（9 种 Investigation Skills：entry_point, root_cause,
    │   architecture, impact, data_lineage, recent_changes, code_explanation,
    │   config_analysis, issue_tracking）
    ├── 预算信号（NORMAL / WARN_CONVERGE / FORCE_CONCLUDE）
    └── 收敛指导（迭代 6-7 次有强证据时停下来）

L4: User Message（查询 + 可选代码上下文）
    └── Brain 的查询 + 可选 code_context（选中的代码片段）
        **永远不要在用户消息中注入 Agent 身份信息**
```

**设计原则：** 遵循 Anthropic 官方提示词工程规范（详见下文 §7.5.1）。

#### 7.5.1 Anthropic 提示词设计规范

以下原则来自 Anthropic 官方文档（[Prompt Engineering Best Practices](https://docs.anthropic.com/en/docs/build-with-claude/prompt-engineering/claude-4-best-practices)、[Context Engineering for Agents](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)），经 Conductor 项目 Eval 验证。

**1. Right Altitude — 找到正确的抽象高度**

Anthropic 的核心原则：不要过于宽泛（模型缺乏方向），也不要过于具体（模型变得脆弱）。

> "The optimal altitude strikes a balance: specific enough to guide behavior effectively, yet flexible enough to provide the model with strong heuristics to guide behavior."

对于推理类任务，倾向于更高的抽象层次：
> "Prefer general instructions over prescriptive steps. A prompt like 'think thoroughly' often produces better reasoning than a hand-written step-by-step plan. Claude's reasoning frequently exceeds what a human would prescribe."

```
# ❌ 过于具体（brittle）
"if you find isFinished or isComplete, use get_callers to trace downstream"

# ❌ 过于宽泛
"investigate the code"

# ✅ Right altitude
"Trace the complete lifecycle from trigger to final outcome — not just the middle steps."
```

**2. 用示例代替规则清单（Examples Over Rule Lists）**

> "Teams will often stuff a laundry list of edge cases into a prompt... We do not recommend this. We recommend working to curate a set of diverse, canonical examples that effectively portray the expected behavior. For an LLM, examples are the 'pictures' worth a thousand words."

3-5 个多样化的示例比长篇规则更有效。用 `<example>` 标签包裹示例以区分于指令。

**3. 解释动机，而非仅下指令（Explain Why）**

> "Claude is smart enough to generalize from the explanation."

不说 "不要用省略号"，而说 "输出将被 TTS 引擎朗读，省略号无法正确发音"。模型能从动机推导出更多正确行为。

**4. 正面表述优先（Positive Framing）**

> "Tell Claude what to do instead of what not to do."

不说 "Don't stop at the middle steps"，说 "Trace the complete lifecycle from trigger to final outcome."

**5. 上下文优先于指令（Context Over Instructions）**

提供工作区布局、项目文档、依赖关系等上下文，让模型自行判断路径。CORE_IDENTITY 中的 `{workspace_layout_section}` 和 `{project_docs_section}` 体现了这一原则。

**6. 多 Agent 角色分工（Role Specialization）**

每个 agent 有独特的调查视角。共享步骤指令会破坏并行价值。

> **实践教训**：向共享的 `explorer_base.md` 添加 "start broad, follow domain model" 策略导致工作流评分从 60% → 25%，因为两个 agent 都走了相同的实现路径。

**7. 工具集保持精简无歧义（Minimal, Unambiguous Tool Sets）**

> "If a human engineer can't definitively say which tool should be used in a given situation, an AI agent can't be expected to do better."

工具输出应 token-efficient，功能不重叠。

**8. 新模型减少强制性语言（Dial Back for Newer Models）**

> "If your prompts were designed to reduce undertriggering on tools, these models may now overtrigger. Where you might have said 'CRITICAL: You MUST use this tool when...', you can use more normal prompting like 'Use this tool when...'"

### 7.6 证据评估器（EvidenceEvaluator）

在 LLM 准备结束时（`stop_reason == "end_turn"`），EvidenceEvaluator 检查答案质量：

```python
def evaluate(self, answer: str, state: AgentState) -> EvidenceResult:
    checks = [
        # 答案里有没有 "file.py:42" 或代码块？
        has_file_references(answer) or has_code_blocks(answer),
        # 调用了至少 2 次工具？
        state.tool_calls_made >= 2,
        # 访问了至少 1 个文件？
        len(state.files_accessed) >= 1,
    ]

    if all(checks) or state.budget_signal == FORCE_CONCLUDE:
        return EvidenceResult.PASS
    else:
        # 注入反馈，让 LLM 继续调查
        return EvidenceResult.RETRY(
            feedback="答案需要包含具体的文件路径和行号引用。请继续调查。"
        )
```

### 7.7 HTTP 接口

```bash
# 同步接口（等待完整结果）
POST /api/context/query
{ "query": "how does auth work?", "room_id": "room-123" }

# SSE 流式接口（实时进度）
POST /api/context/query/stream
# 返回事件流：
# data: {"type": "tool_start", "tool": "grep", "params": {...}}
# data: {"type": "tool_result", "result": "..."}
# data: {"type": "answer", "text": "The auth flow..."}

# 直接执行单个工具（调试用）
POST /api/code-tools/execute/grep
{ "workspace": "/path/to/repo", "params": {"pattern": "authenticate"} }
```

### 7.8 本地模式工具派发（Option E）

当用户在本地打开仓库（非 git-worktree 模式）时，工具调用由后端通过 WebSocket 转发给 Extension 执行。Extension 使用三级派发架构（`localToolDispatcher.ts`）：

```
backend AgentLoopService
  → RemoteToolExecutor
  → WebSocket tool_request → Extension _handleLocalToolRequest
      ↓
  localToolDispatcher.ts（全部原生 TypeScript，零 Python 依赖）
      ├── Tier 1: SUBPROCESS (12) → child_process (rg/git)
      │   grep, read_file, list_files, git_log, git_diff, git_diff_files,
      │   git_blame, git_show, find_tests, run_test, ast_search, get_repo_graph
      │
      ├── Tier 2: AST (6) → web-tree-sitter WASM
      │   file_outline, find_symbol, find_references,
      │   get_callees, get_callers, expand_symbol
      │   (treeSitterService.ts + astToolRunner.ts)
      │
      └── Tier 3: COMPLEX (6) → 原生 TypeScript
          compressed_view, trace_variable, detect_patterns,
          get_dependencies, get_dependents, test_outline
          (complexToolRunner.ts)
```

**Fallback 链：** 每一级失败时自动降级到 legacy subprocess 实现。

**全部原生 TypeScript：** .vsix 分发即用，用户无需安装 Python。

**Grammar WASM 管理：**

Grammar 文件已提交到 `extension/grammars/`，克隆仓库即可使用，无需手动下载。如需更换版本，手动替换 `.wasm` 文件并确保 `web-tree-sitter` npm 包版本与 grammar ABI 匹配（当前锁定在 `extension/package.json`）。

---

## 8. AI 提供商层

### 8.1 统一抽象（AIProvider ABC）

```python
# ai_provider/base.py
class AIProvider(ABC):
    @abstractmethod
    def chat_with_tools(
        self,
        messages: list[dict],   # Bedrock Converse 格式
        tools: list[dict],
        system: str = "",
    ) -> ToolUseResponse: ...

@dataclass
class ToolUseResponse:
    text: str                      # 模型文本输出
    tool_calls: list[ToolCall]     # 模型想调用的工具
    stop_reason: str               # "end_turn" | "tool_use" | "max_tokens"
    usage: TokenUsage              # input/output token 计数

@dataclass
class TokenUsage:
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0    # Anthropic prompt cache
    cache_write_tokens: int = 0
```

**内部消息格式**统一用 Bedrock Converse 格式（content block 数组），OpenAI provider 在内部负责格式转换：

```python
# 所有 provider 接受的消息格式
messages = [
    {"role": "user",      "content": [{"text": "Find auth code"}]},
    {"role": "assistant", "content": [
        {"toolUse": {"toolUseId": "t1", "name": "grep", "input": {...}}}
    ]},
    {"role": "user",      "content": [
        {"toolResult": {"toolUseId": "t1", "content": [{"text": "..."}]}}
    ]},
]
```

### 8.2 三个提供商实现

| 提供商 | 文件 | 底层 API | 备注 |
|--------|------|---------|------|
| `ClaudeBedrockProvider` | `claude_bedrock.py` | Bedrock Converse API | 支持跨区域推理 Profile |
| `ClaudeDirectProvider` | `claude_direct.py` | Anthropic Messages API | 支持 prompt cache |
| `OpenAIProvider` | `openai_provider.py` | OpenAI Chat Completions | 内部转换消息格式 |

### 8.3 ProviderResolver — 自动选优

```python
# ai_provider/resolver.py
class ProviderResolver:
    def resolve(self):
        for name, provider in self._configured_providers():
            try:
                latency = provider.health_check()
                self._healthy.append((name, provider, latency))
            except Exception:
                pass
        # 按延迟排序，选最快的
        self._active = min(self._healthy, key=lambda x: x[2])
```

ProviderResolver 还支持三种角色：
- `get_active_provider()` → 强模型（Sonnet/GPT-4），用于评审综合、重要决策
- `get_explorer_provider()` → 轻量模型（Haiku/Qwen），用于 Explorer Agent，成本低
- `get_classifier_provider()` → 分类专用模型，可用于 LLM 辅助路由分类

**查看当前提供商状态：**
```bash
GET /ai/status
# → {"active_model": "claude-sonnet-4-6", "active_provider": "anthropic", "models": [...]}
```

---

## 9. Git 工作区管理

### 9.1 架构原理

```
用户提供 GitHub PAT + Repo URL
             ↓
后端 GIT_ASKPASS 认证，克隆为 bare repo
             ↓
为每个协作房间创建 git worktree（独立工作目录）
             ↓
VS Code FileSystemProvider 把 worktree 挂载为 conductor:// 虚拟文件系统
```

**为什么用 bare repo？**
Bare repo 只包含 `.git` 内容（没有工作目录），适合服务端存储，支持创建多个 worktree。

**为什么用 worktree？**
多个房间可以共用同一个 bare repo（节省磁盘 + 网络），每个 worktree 是独立的工作目录，在自己的分支上操作，互不影响。

### 9.2 工作区创建流程

```python
# git_workspace/service.py
class GitWorkspaceService:
    async def create_workspace(self, room_id, repo_url, token, branch) -> WorkspaceInfo:
        bare_path = self.workspaces_dir / "repos" / f"{room_id}.git"
        worktree_path = self.workspaces_dir / "worktrees" / room_id

        # 1. 创建临时 GIT_ASKPASS 脚本（echo PAT）
        askpass = self._create_askpass_script(token)
        env = {**os.environ, "GIT_ASKPASS": askpass}

        # 2. 克隆为 bare repo
        await run(["git", "clone", "--bare", repo_url, str(bare_path)], env=env)
        os.unlink(askpass)  # 立即删除，避免 token 泄露

        # 3. 创建 worktree，在专属分支上
        branch_name = f"session/{room_id}"
        await run(["git", "worktree", "add", "-b", branch_name, str(worktree_path)],
                  cwd=str(bare_path))

        return WorkspaceInfo(room_id=room_id, worktree_path=worktree_path, ...)
```

### 9.3 路径沙箱

所有代码工具都通过 `_resolve()` 确保路径不会逃出 worktree：

```python
def _resolve(workspace: str, rel_path: str) -> Path:
    ws = Path(workspace).resolve()
    target = (ws / rel_path).resolve()
    if not str(target).startswith(str(ws)):
        raise ValueError(f"路径越界: {rel_path}")
    return target
```

所有工具的输入输出均使用**相对路径**，绝不向外暴露服务器的绝对路径。

### 9.4 工作区接口

```bash
# 创建工作区（克隆 + worktree）
POST /api/git-workspace/workspaces
{ "room_id": "room-123", "repo_url": "https://github.com/...", "token": "ghp_..." }

# 列出工作区
GET /api/git-workspace/workspaces

# 同步（fetch + merge 远端）
POST /api/git-workspace/workspaces/{room_id}/sync

# 提交变更
POST /api/git-workspace/workspaces/{room_id}/commit
{ "message": "fix: auth token expiry" }

# 推送
POST /api/git-workspace/workspaces/{room_id}/push

# 删除工作区
DELETE /api/git-workspace/workspaces/{room_id}
```

---

## 10. 聊天系统

### 10.1 房间模型与持久化

每个协作会话是一个**房间**（room），由 `room_id` 标识。消息使用**写穿透**模型：

```
发消息 → Redis 热缓存（6h TTL，即写即读）
       → ChatPersistenceService（micro-batch，每 3 条或 5 秒写入 Postgres）
```

**Postgres 是 source of truth**。重连时从 Postgres 加载历史，Redis 是读缓存。

```python
# chat/manager.py
class ConnectionManager:
    def __init__(self):
        # room_id → [WebSocket, ...]
        self.active_connections: dict[str, list[WebSocket]] = {}
        # room_id → [message, ...]（内存缓存，服务重启后从 Postgres 重建）
        self.room_messages: dict[str, list[dict]] = {}

    async def broadcast(self, room_id: str, msg: dict) -> None:
        dead = []
        for ws in self.active_connections.get(room_id, []):
            try:
                await ws.send_json(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.active_connections[room_id].remove(ws)
```

### 10.2 WebSocket 协议

连接地址：`ws://<host>/ws/chat/{room_id}`

**服务端 → 客户端：**

| type | 触发时机 |
|------|---------|
| `connected` | 连接建立，返回 user_id 和 role |
| `history` | 连接建立时的历史消息 |
| `message` | 新消息（chat / code_snippet / stack_trace / ai_message）|
| `typing` | 有人正在输入 |
| `read_receipt` | 消息已被某用户看到 |
| `user_joined` / `user_left` | 成员进出房间 |
| `session_ended` | Host 结束会话 |

**客户端 → 服务端：**

| type | 动作 |
|------|------|
| `join` | 加入房间 |
| `message` | 发送消息 |
| `typing` | 正在输入 |
| `read` | 标记消息为已读 |
| `end_session` | 结束会话（仅 host）|

### 10.3 AI 消息注入

AI 回复不经过 WebSocket 发送，而是通过独立的 HTTP 端点注入，这样后端可以花时间生成而不阻塞：

```python
@router.post("/chat/{room_id}/ai-message")
async def post_ai_message(room_id: str, req: AiMessageRequest):
    provider = get_resolver().get_active_provider()
    response = await provider.chat(messages=req.context_messages)
    msg = {"type": "ai_message", "content": response.text}
    await manager.broadcast(room_id, msg)
    return {"status": "sent"}
```

### 10.4 断线重连

Extension 重连时通过 `since` 参数获取错过的消息，避免重复加载：

```python
@router.get("/chat/{room_id}/history")
async def get_history(room_id: str, since: str | None = None, limit: int = 50):
    messages = manager.get_history(room_id)
    if since:
        messages = [m for m in messages if m["timestamp"] > since]
    return messages[-limit:]
```

历史记录包含 `codeSnippet` 字段（对 `code_snippet` 类型消息），确保重连后代码片段能正确渲染。

### 10.5 删除房间

```python
# DELETE /chat/{room_id}
# 清除：内存历史、Redis 缓存、Postgres 记录、关联文件、审计日志
@router.delete("/chat/{room_id}")
async def delete_room(room_id: str):
    manager.clear_room(room_id)          # 内存
    await redis_store.delete_room(...)   # Redis
    await persistence.delete_room(...)   # Postgres
    ...
```

Extension 在用户退出房间时调用此接口，彻底清除历史。

---

## 11. Extension UI 流程

这一节解释 Extension 侧的两种使用模式，以及它们是如何与后端交互的。新工程师调试 UI 问题时必读。

### 11.1 两种会话模式

```
┌─────────────────────────────────────────┐
│  Extension 启动 → 选择模式              │
│                                         │
│  [在线模式 (Online)]                    │
│   └── 加载房间列表 (GET /chat/rooms)    │
│   └── 加入已有房间 or 创建新房间        │
│   └── Git Workspace 在服务器端管理     │
│                                         │
│  [本地模式 (Local)]                     │
│   └── 自动注册本地工作区               │
│       (POST /api/git-workspace/         │
│        workspaces/local)               │
│   └── 工具通过 WebSocket 转发给        │
│       Extension 本地执行               │
└─────────────────────────────────────────┘
```

**关键区别：**

| | 在线模式 | 本地模式 |
|---|---|---|
| Git 工作区 | 后端 bare clone + worktree | 用户本地目录 |
| 工具执行 | 后端 Python | Extension TypeScript（localToolDispatcher） |
| 聊天历史 | Postgres（持久） | chatLocalStore（VS Code globalState） |
| AI 调用 | 走后端 `/api/context/query` | 同上（都走后端） |

### 11.2 在线模式：房间列表加载

```typescript
// StatePanels.tsx — ReadyToHostPanel 模式切换
// 用户在 Local/Online tab 之间切换
const [mode, setMode] = useState<"local" | "online">("local");
// 选择在线模式时:
        loadOnlineRooms();  // 发消息给 Extension Host
    }
}

// extension.ts — getOnlineRooms handler
case 'getOnlineRooms':
    const resp = await fetch(`${backendUrl}/chat/rooms?email=${userEmail}`);
    const rooms = await resp.json();
    panel.webview.postMessage({ command: 'onlineRooms', rooms });
```

### 11.3 本地模式：自动注册工作区

```typescript
// extension.ts — _handleStartSession()
// 用户按下"New Session"后自动调用（无需手动"Use Local"按钮）
async _handleStartSession() {
    const folders = vscode.workspace.workspaceFolders;
    if (!folders?.length) {
        // 没有打开工作区时弹警告
        const action = await vscode.window.showWarningMessage(
            'No workspace folder open.',
            'Open Folder'
        );
        return;
    }
    // 注册本地工作区到后端
    await fetch(`${backendUrl}/api/git-workspace/workspaces/local`, {
        method: 'POST',
        body: JSON.stringify({ room_id, path: folders[0].uri.fsPath }),
    });
}
```

### 11.4 本地工具派发（localToolDispatcher）

本地模式下，Agent 的工具调用流程：

```
后端 AgentLoopService
  → RemoteToolExecutor（检测到是本地会话）
  → WebSocket tool_request 消息
  → Extension._handleLocalToolRequest()
  → localToolDispatcher.ts（纯 TypeScript，零 Python 依赖）
      ├── grep/git/read/glob 等 13 个子进程工具
      ├── file_outline/find_symbol 等 6 个 AST 工具（web-tree-sitter）
      ├── compressed_view/trace_variable 等 6 个复杂工具
      └── file_edit/file_write 2 个文件编辑工具（fileEditRunner.ts）
      注：Jira 工具 (5) 和浏览器工具 (6) 始终在后端执行，不走本地派发
  → tool_response 消息返回后端
  → 后端继续 Agent 循环
```

---

## 12. 文件共享

### 12.1 上传流程

```
VS Code WebView（浏览器沙盒）
  ↓  无法直接发 HTTP，通过 vscode.postMessage
Extension Host（Node.js）
  ↓  multipart POST /api/files/upload
Backend → PostgreSQL 记录元数据（file_id、room_id、sha256、原始文件名）
  ↓  返回 file_id
Extension 通过 WebSocket 广播 file_id
  ↓  其他成员收到后请求 GET /api/files/{file_id}
```

**为什么由 Extension Host 代理？** VS Code WebView 运行在浏览器沙盒里，无法直接发任意 HTTP 请求（CORS 限制）。Extension Host 是 Node.js 进程，没有这个限制。

### 12.2 去重机制

上传时计算 SHA-256，如果哈希已存在则直接返回已有的 `file_id`，不重复存储文件：

```python
def upload_file(self, room_id: str, filename: str, content: bytes) -> FileRecord:
    sha256 = hashlib.sha256(content).hexdigest()
    existing = self.db.query("SELECT * FROM files WHERE sha256 = ?", [sha256]).fetchone()
    if existing:
        return FileRecord.from_row(existing)  # 去重
    file_id = str(uuid4())
    path = self.upload_dir / room_id / file_id
    path.write_bytes(content)
    self.db.execute("INSERT INTO files VALUES ...", [file_id, room_id, sha256, ...])
    return FileRecord(file_id=file_id, ...)
```

---

## 13. 审计日志与 TODO 管理

### 13.1 存储层

审计日志和 TODO 数据持久化在 PostgreSQL，与其他业务数据共用同一个数据库实例。

### 13.2 审计日志

记录每次用户接受/拒绝 AI 建议变更的操作：

```python
# audit/service.py
service = AuditLogService.get_instance()  # 单例

service.log_apply(AuditLogCreate(
    room_id="room-123",
    changeset_hash=sha256(changeset),   # 变更集的指纹，可用于追溯
    applied_by="user-456",
    mode="manual",                       # "manual" | "auto"
))

# 查询某房间的审计历史
logs = service.get_logs(room_id="room-123")
```

**Schema：**
```sql
CREATE TABLE audit_logs (
    id             INTEGER PRIMARY KEY,
    room_id        VARCHAR,
    changeset_hash VARCHAR,
    applied_by     VARCHAR,
    mode           VARCHAR,
    timestamp      TIMESTAMP
)
```

### 13.3 TODO 追踪

每个房间有独立的 TODO 列表，支持完整 CRUD：

```bash
GET    /todos/{room_id}           # 列出房间 TODO
POST   /todos/{room_id}           # 创建 TODO
PATCH  /todos/{room_id}/{todo_id} # 更新状态/文本
DELETE /todos/{room_id}/{todo_id} # 删除
```

TODO 持久化在 PostgreSQL，服务重启后不丢失。

---

## 14. 身份认证

### 14.1 AWS SSO（设备授权流程）

```yaml
# conductor.settings.yaml
sso:
  enabled: true
  start_url: "https://d-xxxx.awsapps.com/start"
  region: "eu-west-2"
```

流程：
1. 用户调用 `POST /auth/sso/start` → 获得设备码和验证 URL
2. 用户在浏览器打开 URL，在 AWS 托管页面登录
3. Extension 轮询 `POST /auth/sso/poll` 直到登录完成
4. 获得 session token，缓存在 `globalState`（带 TTL）

### 14.2 Git 凭证（PAT）

Personal Access Token 通过 `GIT_ASKPASS` 机制传给 Git（见第 9 节）。后端从不持久化 PAT，只在 git 操作期间在内存中保存。

---

## 15. Jira 集成

Conductor 的 Jira 集成分为三层：OAuth 连接 → REST API → Agent 工具。用户通过 `@AI /jira` 命令与 Jira 交互，Brain 调度 issue_tracking 技能的 Agent，Agent 使用 5 个 Jira 工具自主完成任务。

### 15.1 架构全景

```
用户: @AI /jira create Fix login bug
      ↓
Extension: /jira transform → "[jira] Create a Jira ticket for: Fix login bug..."
      ↓
POST /api/context/query/stream → Brain（Sonnet）
      ↓
Brain: create_plan → dispatch_agent(
    tools=["grep", "read_file", "jira_search", "jira_create_issue", ...],
    skill="issue_tracking", model="strong", budget_tokens=500000)
      ↓
Sub-agent:
  1. grep/read_file 分析代码 → 收集 affected files
  2. jira_search 检查重复票
  3. ask_user 确认票详情（summary/project/priority/component）
  4. jira_create_issue 创建票 → 返回 browse_url
      ↓
Brain: 综合结果，返回包含 Jira 链接的最终回答
      ↓
Chat UI: 自动将 DEV-123 等 ticket key 渲染为可点击的 Jira 链接
```

### 15.2 四种用户意图

`/jira` slash command 根据用户输入检测意图并生成对应的查询：

| 命令 | 意图 | Agent 行为 |
|------|------|-----------|
| `/jira create Fix login bug` | CREATE | 分析代码 → 检查重复 → 评估复杂度 → ask_user 确认 → 创建票 → 返回链接 |
| `/jira DEV-123` | CONSULT | 拉取票详情 → 读相关代码 → 解释要做什么 + 建议实现方案 |
| `/jira my tickets` | SEARCH | JQL 搜索 → 按优先级分组 → 建议先做哪些 |
| `/jira my sprint` | SEARCH | openSprints() JQL → 按状态分组 → 高亮 blocker |
| `/jira blockers` | SEARCH | 高优先级 + blocked 标签 → 建议先解除什么 |
| `/jira workload` | SEARCH | 全量已分配票 → 按状态和优先级统计 → 建议 focus plan |
| `/jira update DEV-123 ...` | UPDATE | ask_user 确认 → 状态转换/评论/字段更新 |
| `/jira` (空) | SEARCH | 列出所有未完成票，按优先级分组 |

### 15.3 OAuth 3LO 流程

```
用户点击 "Connect Jira"
      ↓
GET /api/integrations/jira/authorize-url
      → 生成 Atlassian 授权 URL + state（防 CSRF）
      ↓
用户在浏览器登录 Atlassian，授权 Conductor 访问
      ↓
Atlassian 重定向到 redirect_uri → 后端交换 code → tokens + cloud_id
      ↓
浏览器打开 vscode://publisher.conductor/jira/callback?connected=true
      ↓
Extension JiraUriHandler:
      → POST /api/integrations/jira/callback 交换 code
      → GET /api/integrations/jira/tokens 获取 token
      → JiraTokenStore 存入 SecretStorage + .conductor/jira.json
      → 启动时自动恢复连接（无需重新授权）
```

**配置（conductor.secrets.yaml）：**

```yaml
jira:
  client_id: "your-atlassian-client-id"
  client_secret: "your-atlassian-client-secret"
  redirect_uri: "https://your-backend-url/api/integrations/jira/callback"
  teams:
    - id: "uuid-1234"
      name: "Platform"
```

**配置（conductor.settings.yaml）：**

```yaml
jira:
  enabled: true
  allowed_projects: ["DEV", "FN", "FO", "HELP", "PT", "REN"]
  branch_formats:
    feature: "feature/{ticket}-{content}"
    bugfix: "bugfix/{ticket}-{content}"
```

### 15.4 5 个 Agent 工具

这些工具注册在 `JIRA_TOOL_REGISTRY` 中，由 Brain dispatch 的 sub-agent 调用：

```python
# jira_search — 支持 JQL、自由文本、快捷方式
result = jira_search(workspace, query="my sprint")
# 快捷方式自动展开：
#   "my tickets"  → assignee = currentUser() AND status NOT IN (Done, Closed, Resolved)
#   "my sprint"   → assignee = currentUser() AND sprint IN openSprints()
#   "blockers"    → priority IN (Highest, Blocker) OR labels = blocked

# jira_get_issue — 完整详情（ADF→纯文本转换）
result = jira_get_issue(workspace, issue_key="DEV-123")
# → summary, description, status, priority, assignee, components, comments, subtasks

# jira_create_issue — ADF 描述 + 代码块 + 子任务
result = jira_create_issue(workspace,
    project_key="DEV", summary="Fix auth bug",
    description="Token expiry...\n```python\ndef refresh()...\n```",
    parent_key="DEV-100")  # 在 Epic 下创建子任务

# jira_update_issue — 安全：Done/Closed/Resolved 被阻止
result = jira_update_issue(workspace,
    issue_key="DEV-123", transition_to="In Progress", comment="Started work")
# ⚠️ transition_to="Done" → 返回错误，要求用户手动关闭

# jira_list_projects — 列出可访问项目
result = jira_list_projects(workspace)
```

### 15.5 issue_tracking 调查技能

Brain dispatch sub-agent 时注入 `issue_tracking` 技能（L3 prompt），指导 Agent 按意图执行：

- **CREATE**：先调查代码 → 检查重复 → 评估复杂度 → 用 `jira_project_guide.yaml` 映射文件路径到项目+组件 → `ask_user` 确认 → 创建
- **CONSULT**：拉票 → 读相关代码 → 输出结构化报告（ticket header + 代码映射 + 建议方案）
- **SEARCH**：构建 JQL → 按优先级分组 → 建议 focus
- **UPDATE**：`ask_user` 确认 → 执行更新

**项目映射（`config/jira_project_guide.yaml`）：**

Agent 根据 git diff 文件路径自动匹配 Jira 项目和组件：

```yaml
projects:
  DEV:
    description: "Core engineering"
    repos:
      abound-server:
        rules:
          - paths: ["abound-server/"]
            component: "JBE"
          - paths: ["CDE/"]
            candidates: ["Mortgage", "Decision Engine"]  # Agent 根据上下文选择
        default_component: "JBE"
```

### 15.6 TODO↔Ticket 双向同步

Extension 提供 TODO 与 Jira 票的双向关联：

```
┌─ Backlog（3 个分区）──────────────────────┐
│                                            │
│  📎 Linked（TODO + Jira 关联）             │  ← 可拖拽到 AI Working Space
│    {jira:DEV-123} Fix auth timeout         │
│    Status: In Progress │ Priority: High    │
│                                            │
│  📝 Code TODOs（无 Jira 关联）             │
│    // TODO: refactor this function         │
│    src/auth.ts:42                          │
│                                            │
│  🔵 Jira Tickets（当前用户未完成票）        │
│    DEV-456: Add retry logic                │
│    Status: To Do │ Priority: Medium        │
│                                            │
├─ AI Working Space ─────────────────────────┤
│  将 Linked 卡片拖入此区域让 AI 分析        │
└────────────────────────────────────────────┘
```

**关键组件：**

| 文件 | 职责 |
|------|------|
| `ticketProvider.ts` | `ITicketProvider` 接口 + `JiraTicketProvider` 实现（批量状态查询、my tickets）|
| `todoScanner.ts` | 工作区 TODO 扫描器（支持 `{jira:KEY}` 标签 + 裸 KEY 模式，43+ 文件类型）|
| `webview-ui/src/components/tasks/TasksTab.tsx` | 3 分区 Backlog UI + AI Working Space + drag-and-drop |

**`{jira:KEY}` 标签：**

在代码中的 TODO 注释里添加 `{jira:DEV-123}` 标签，即可与 Jira 票关联：

```python
# TODO: Fix token expiry check
# TODO_DESC: {jira:DEV-123} Token refresh not triggered before expiry
```

### 15.7 Ticket 创建 UI

Extension 提供 Jira 票创建表单（`showJiraModal(prefill)`）：

- **Component 多选**：chip/tag UI + 下拉筛选
- **Agent 预填充**：agent 分析代码后预填所有字段
- **用户可编辑**：所有字段在提交前可修改
- **确认 modal**：背景模糊 overlay，Create / Cancel 按钮

### 15.8 Chat 中的 Jira 渲染

- **Ticket key 自动链接**：AI 回答中的 `DEV-123` 自动渲染为可点击的 Jira 链接（需已连接 Jira）
- **结构化输出**：Agent 按 skill prompt 输出格式化的 ticket header + 代码映射 + 优先级分组

### 15.9 REST API 端点

```bash
# OAuth
GET  /api/integrations/jira/authorize-url  # 获取授权 URL
GET  /api/integrations/jira/callback        # OAuth 浏览器重定向处理
POST /api/integrations/jira/callback        # Extension 直接交换 code
GET  /api/integrations/jira/status          # 当前连接状态
POST /api/integrations/jira/disconnect      # 断开连接
GET  /api/integrations/jira/tokens          # 获取 token（Extension 本地持久化用）
POST /api/integrations/jira/refresh         # 刷新 token

# CRUD
GET  /api/integrations/jira/projects        # 列出可访问项目
GET  /api/integrations/jira/issue-types     # 查询项目的 issue 类型
GET  /api/integrations/jira/create-meta     # 创建 issue 所需字段元数据
GET  /api/integrations/jira/search          # JQL 文本搜索（?q=...&maxResults=10）
GET  /api/integrations/jira/undone          # 当前用户未完成票（快捷查询）
GET  /api/integrations/jira/issue/{key}     # 获取单个 issue 完整详情
POST /api/integrations/jira/issues          # 创建 issue
POST /api/integrations/jira/issue/{key}/transition  # 状态转换
```

### 15.10 Extension Token 持久化

Token 不再仅存内存。Extension 通过 `JiraTokenStore` 本地持久化：

| 数据 | 存储位置 | 安全级别 |
|------|---------|---------|
| access_token, refresh_token | VS Code SecretStorage（OS 钥匙链）| 加密 |
| expires_at, cloud_id, site_url | `.conductor/jira.json` | 明文（非敏感）|

启动时自动从本地恢复连接，过期时自动刷新 token，无需用户重新授权。

### 15.11 测试覆盖

| 文件 | 测试数 | 覆盖 |
|------|-------|------|
| `test_jira_router.py` | 45 | OAuth 3LO + REST API 端点 |
| `test_jira_service.py` | 48 | Token 生命周期 + API 调用 |
| `test_jira_tools.py` | 21 | Agent 工具（search/create/update/get_issue）|
| `ticketProvider.test.ts` | 93 | ITicketProvider + tag 解析 + 状态查询 |

### 15.12 常见问题

| 症状 | 原因 | 解法 |
|------|------|------|
| `Jira integration is not enabled` | `jira.client_id` 未在 secrets 中配置 | 填写 `conductor.secrets.yaml` → `make app-restart` |
| 连接后 ticket key 不自动链接 | `jiraSiteUrl` 未设置 | 确认 Jira 已连接（JiraModal 组件收到 `jiraConnected` 后设置 siteUrl）|
| `jira_update_issue` 无法关闭票 | 安全阻止 Done/Closed/Resolved | 设计如此——agent 不应自动关闭票，需用户在 Jira 手动操作 |
| Team 字段找不到 | `customfield_10001` 因 Jira 实例不同而变化 | 先调用 `GET /create-meta` 确认 team_field_key |
| "my sprint" 返回空 | 项目未配置 Sprint board | 确认 Jira 项目启用了 Scrum board 且有活跃 Sprint |

---

## 16. LangExtract 集成

`langextract/` 为 Google 的 [langextract](https://github.com/google/langextract) 库提供 Bedrock 多厂商插件。

### 16.1 BedrockCatalog — 动态模型发现

```python
from app.langextract.catalog import BedrockCatalog

catalog = BedrockCatalog(region="eu-west-2")
catalog.refresh()  # 调用 list_foundation_models() + list_inference_profiles()

# 按厂商分组（UI 下拉菜单用）
models = catalog.models_by_vendor()
# → {"Anthropic": ["claude-sonnet-4-6", ...], "Amazon": ["nova-pro", ...], ...}
```

`BedrockCatalog` 自动处理跨区域推理 Profile 的 `eu.` 前缀，无需手动构造 ID。

### 16.2 LangExtractService

```python
from app.langextract.service import LangExtractService
from langextract.data import ExampleData, Extraction

svc = LangExtractService(
    model_id="claude-sonnet-4-20250514",
    region="eu-west-2",
    catalog=catalog,
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
```

`BedrockLanguageModel` 注册为 `@router.register()`，使 `lx.extract(model_id="...")` 自动使用 Bedrock。`ClaudeLanguageModel` 作为向后兼容别名保留。

---

## 17. Langfuse 可观测性

Langfuse 提供嵌套执行树、成本追踪和延迟分析，是 SessionTrace 的补充：

| | SessionTrace | Langfuse |
|---|---|---|
| 数据 | 工具参数、思考文本、预算信号 | 成本、延迟、嵌套树 |
| 存储 | 本地 JSON 文件 | Postgres（自托管）|
| 界面 | 无（离线分析）| Web UI（团队可视化）|
| 开销 | ~0（本地写文件）| ~0.1ms（异步 SDK）|

### 17.1 本地启动 Langfuse

```bash
# 启动 Langfuse + PostgreSQL（端口 3001）
make langfuse-up

# 查看日志
make langfuse-logs

# 关闭
make langfuse-down
```

访问 `http://localhost:3001`，创建项目并获取 API Keys。

### 17.2 配置

```yaml
# conductor.settings.yaml
langfuse:
  enabled: true
  host: "http://localhost:3001"

# conductor.secrets.yaml
langfuse:
  public_key: "pk-..."
  secret_key: "sk-..."
```

### 17.3 追踪结构

PR 评审的 Langfuse 追踪树示例：

```
brain: pr_review                         45.2s  $0.38
├── transfer_to_brain("pr_review")       0.1ms
├── PRBrainOrchestrator phase 1: pre-compute (parse_diff, classify_risk, prefetch_diffs)
├── dispatch agent: correctness          35.2s  $0.12
│   └── agent: correctness (explorer)
│       ├── llm_call (generation)         1.2s   →工具: grep
│       ├── llm_call (generation)         0.9s   →工具: read_file
│       └── ... (共 18 次工具调用)
├── dispatch agent: security             32.8s  $0.09
│   └── ...（与 correctness 并行）
├── stage: arbitrate                      3.8s  $0.08
│   └── agent: arbitrator (judge)
└── stage: synthesize                     2.3s  $0.03
    └── agent: review_synthesizer (judge)
```

### 17.4 @observe 装饰器

在工作流代码中使用零侵入的装饰器：

```python
from app.workflow.observability import observe

# 当 Langfuse 禁用时，这些装饰器是零开销的 no-op
@observe(name="agent")
async def _run_agent(self, agent: AgentConfig, ...):
    ...
```

禁用 Langfuse 时（`langfuse.enabled: false` 或包未安装），`@observe` 直接返回原函数，没有任何开销。

---

## 18. 评估系统 (eval/)

`eval/` 是独立的三套评估套件（通过 `.dockerignore` 排除在 Docker 镜像之外）：

```
eval/
├── code_review/          代码评审质量（12 个 requests legacy + 50 个 Greptile benchmark）
├── agent_quality/        Agent Loop 答案质量（基线对比）
└── tool_parity/          Python vs TypeScript 工具输出对比
```

详细文档见 `eval/README.md`。

### 18.1 代码评审评估（code_review/）

在真实开源代码库中植入已知 bug（git patch），运行完整的 PR Brain / `CodeReviewService` 管线，检查发现的结果是否匹配预期。

两套 case 并存：

- **12 个 requests-v2.31.0 legacy cases** — 最早的自产 case，难度梯度可控，适合单元式快速回归。
- **50 个 Greptile benchmark cases** — 对齐 Greptile 公开的 AI Code Review Benchmark（sentry / cal.com / grafana / keycloak / discourse 五个大型 OSS 项目各 10 个真实 bug-fix PR），用于横向对比 Cursor / Copilot / CodeRabbit 等商用 reviewer 的 `catch_rate`。详细原理和数据管线见 `eval/code_review/GREPTILE_BENCHMARK.md`。

```bash
cd backend

# 一次性启动 Greptile 数据集（克隆 5 个 fork + 物化 50 个 base snapshot + 重新生成 patch）
python ../eval/code_review/setup_greptile_dataset.py

# 跑全部 legacy + Greptile（PR Brain 管线，生产路径）
python ../eval/code_review/run.py --brain \
    --provider bedrock \
    --model "eu.anthropic.claude-sonnet-4-6" \
    --explorer-model "eu.anthropic.claude-haiku-4-5-20251001-v1:0"

# 只跑 Greptile 50 case，开启 verbose 逐 finding 打印
python ../eval/code_review/run.py --brain --filter greptile- --verbose

# 只跑某个子集（快速验证）
python ../eval/code_review/run.py --filter "requests-001" --no-judge
python ../eval/code_review/run.py --filter "greptile-sentry" --no-judge

# 保存当前结果为基线（下次对比用）
python ../eval/code_review/run.py --save-baseline

# 黄金标准：直接运行 Claude Code CLI（质量上限）
python ../eval/code_review/run.py --gold --gold-model opus --save-baseline
```

**12 个 requests legacy 用例：** 4 简单、5 中等、3 困难（基于 requests v2.31.0）。

**50 个 Greptile 用例：** 44 个从 `greptile-apps[bot]` 的 inline review 评论自动导入，6 个人工标注（bot 没留可用 anchor）。ground truth 是 `(file, line, severity, category)`，scorer 用 **catch_rate** 作为头部指标（对齐 Greptile 报告的 82%）。

**评分维度：** 召回率 (35%)、精确率 (20%)、严重程度准确性 (15%)、位置准确性 (10%)、修复建议 (10%)、上下文深度 (10%)，外加 **catch_rate** —— 对每个 case 是否至少在正确的 `(file, line)` 上发现一个预期 finding，作为 Greptile-style 的头部指标。

#### 18.1.1 Greptile 数据管线：两个关键技术

这一套 50 case 能跑起来依赖两个非常具体的技术决策，每一个都值得单独记住。完整推导过程见 `eval/code_review/GREPTILE_BENCHMARK.md` §7 / §8 以及 Appendix A（git object model 速成）。

**(1) Merge-base patch 对齐（数据集 bootstrap，`materialize_greptile_bases.py`）**

直觉流程 —— 把 GitHub API 返回的 `.diff` 对着 `base_sha` 打上去 —— 在 Greptile fork 上**经常失败**。原因是 GitHub 的 `.diff` endpoint 计算的是 `merge-base(base_sha, head_sha)` → `head_sha`，而 Greptile 的 fork 会定期同步 upstream，导致 `base_sha` 包含 PR 不知道的新 commit，`git apply` 直接报 "patch does not apply"。

解决办法：snapshot 和 patch **都从同一份本地 fork clone 里派生**，锁定在 `merge-base`：

```bash
# 1. blobless clone fork（--filter=blob:none 只拉 commit + tree，blob 按需）
git clone --filter=blob:none https://github.com/ai-code-review-evaluation/sentry-greptile.git

# 2. 算 merge-base（纯 commit 图算法，不需要 blob）
merge_base=$(git merge-base $base_sha $head_sha)

# 3. 用 git archive 从 merge_base 物化纯源码快照（此时按需拉 blob）
git archive --format=tar $merge_base | tar -x -C repos/greptile_bases/sentry/001/

# 4. 用本地 git diff 重新生成 patch（两端 tree 已有，补拉差异 blob）
git diff $merge_base $head_sha > cases/greptile_sentry/patches/001.patch
```

这样生成的 patch 和 snapshot 天然一致，`git apply` 一定能打上。**泛化启示**：任何时候你要"重放一个 PR"，不要混用 API diff 和 API `base_sha`，必须锁定到同一份本地 git 状态的一对 commit 上。

**(2) Hardlink workspace（每个 case 的 per-run 准备，`runner.py::setup_workspace`）**

跑 50 case 时每 case 都要一个独立可写的 workspace（要 `git init` + `git apply` + `git commit`），但不能污染共享的 base snapshot（6 GB × 50 = 300 GB 根本放不下，而且我们希望 snapshot 复用跨多次 run）。

朴素方案是 `shutil.copytree` —— sentry 17K 文件要跑 ~90 秒/case，50 case 就是 75 分钟光 copy，完全破坏交互开发体验。

`setup_workspace` 用的办法是 **硬链接 + 依赖 atomic write 触发 per-file COW**：

```python
def _link_or_copy(s, d):
    try:
        os.link(str(s), str(d))       # 同 inode，瞬时完成
    except OSError:
        shutil.copy2(str(s), str(d))  # 跨文件系统时 fallback
```

硬链接只是往目录里加一条新 `(name → inode)` 条目，零字节复制。17K 文件从 ~90 秒压到 ~1 秒。

关键洞察是：`git apply`（以及几乎所有像样的文本编辑工具）用 **write-new-file + rename** 模式修改文件 —— 写一个全新 inode 的临时文件，然后 rename 覆盖目标。rename 只动了 workspace 这一侧的目录条目，**snapshot 那一侧的 inode 完全没被碰**。硬链接只在"被改的那几个文件"处断开，其他几万个文件继续共享 snapshot 的 inode。

对一个改 3 个文件的 PR 而言，每个 workspace 实际独占的磁盘只有 3 个新 inode（加上 `.git/`），其他 17K 文件全部是免费共享。`rmtree` 清理时 snapshot 一字节都不掉。

**泛化启示**：任何时候你要给多个消费者准备"一个只读大数据集的独立可写视图"—— test runner、CI build cache、container rootfs、eval harness —— 硬链接 + 工具的 atomic-write 惯例就能几乎零成本地给你 copy-on-write，完全不需要 btrfs / overlayfs 这种特殊文件系统支持。pnpm 的 `node_modules store`、Nix 的 `/nix/store`、`cp --link`、`rsync --link-dest`、ccache 都是这个模式的变种。

两个技术点的共同哲学：**信任 git 的对象模型 + 信任 POSIX 文件系统的惯例，尽量不引入新抽象**。

### 18.2 Agent 质量评估（agent_quality/）

对 Agent Loop 的回答质量进行端到端测试，与基线答案对比：

```bash
cd backend

# 运行全部基线用例
python ../eval/agent_quality/run.py

# 运行特定用例
python ../eval/agent_quality/run.py --case abound_render_approval

# 对比直接 Agent vs Workflow（多 Agent）
python ../eval/agent_quality/run.py --compare
```

基线文件在 `eval/agent_quality/baselines/*.json`，每个 JSON 定义 `workspace`、`question`、`required_findings`（含权重和匹配模式）。

### 18.3 工具一致性评估（tool_parity/）

对比 Python（tree-sitter）和 TypeScript（extension）实现的工具输出是否一致：

```bash
cd backend

# 生成 Python 基线
python ../eval/tool_parity/run.py --generate-baseline

# 对比 TS 输出（需要 extension 运行）
python ../eval/tool_parity/run.py --compare
```

---

## 19. 测试规范

### 19.1 运行测试

```bash
cd backend
pytest                                            # 全量 1300+ 测试
pytest tests/test_agent_loop.py -v               # Agent Loop
pytest tests/test_code_tools.py -v               # 代码工具
pytest tests/test_budget_controller.py -v        # 预算控制器
pytest tests/test_compressed_tools.py -v         # 压缩视图工具
pytest tests/test_langextract.py -v              # LangExtract
pytest tests/test_repo_graph.py -v               # 依赖图
pytest tests/test_chat_persistence.py -v         # 聊天持久化
pytest tests/test_browser_tools.py -v            # 浏览器工具（Playwright，mocked）
pytest --cov=. --cov-report=html                 # 覆盖率报告

# 工具一致性验证（Python ↔ TypeScript）
make test-parity                                  # 合约检查 + 形状验证 + 子进程验证
```

**主要测试文件：**

| 文件 | 数量 | 覆盖内容 |
|------|------|---------|
| `test_code_tools.py` | 139 | 全部 42 工具 + 调度器 + 多语言 |
| `test_agent_loop.py` | 55 | Agent Loop + 四层 Prompt + 完整性检查 |
| `test_brain.py` | 64 | Brain 编排器 + dispatch 模式 |
| `test_jira_tools.py` | 21 | Jira Agent 工具 |
| `test_jira_service.py` | 48 | Jira OAuth + API 服务 |
| `test_jira_router.py` | 45 | Jira REST 端点 |
| `test_budget_controller.py` | 20 | 预算信号转换、追踪、边界情况 |
| `test_session_trace.py` | 23 | SessionTrace JSON 保存/加载 |
| `test_evidence.py` | 19 | 证据评估器质量门控 |
| `test_symbol_role.py` | 24 | 符号角色分类 + 装饰器检测 |
| `test_output_policy.py` | 19 | 每工具截断策略、预算自适应 |
| `test_compressed_tools.py` | 24 | compressed_view、module_summary、expand_symbol |
| `test_langextract.py` | 57 | Bedrock Provider、Catalog、Service |
| `test_repo_graph.py` | 72 | Parser + 依赖图 + PageRank |
| `test_config_new.py` | 27 | Config + Secrets |
| `test_chat_persistence.py` | — | ChatPersistenceService micro-batch 写入、刷新计时器 |
| `test_browser_tools.py` | — | 浏览器工具（Playwright service mocked）|

### 19.2 测试基础设施

**`conftest.py` 中央 stub：** cocoindex、litellm、sentence_transformers、sqlite_vec 等库被 stub 掉，避免需要安装所有外部依赖才能跑测试。

**代码工具测试用真实文件系统：**

```python
def test_grep(tmp_path):
    # 创建真实文件
    (tmp_path / "app.py").write_text("def authenticate(user): ...")
    result = execute_tool("grep", str(tmp_path), {"pattern": "authenticate"})
    assert result.success
    assert "app.py" in result.data
    assert result.data["app.py"][0]["line"] == 1
```

**Agent Loop 测试用 MockProvider：**

```python
class MockProvider(AIProvider):
    def __init__(self, responses: list[ToolUseResponse]):
        self._it = iter(responses)

    def chat_with_tools(self, messages, tools, system="") -> ToolUseResponse:
        return next(self._it)

async def test_agent_loop_basic():
    provider = MockProvider([
        # 第一轮：LLM 决定调用 grep
        ToolUseResponse(tool_calls=[ToolCall(id="t1", name="grep",
                                             input={"pattern": "authenticate"})]),
        # 第二轮：LLM 看到结果后给出答案
        ToolUseResponse(text="Auth is in auth/router.py:42", stop_reason="end_turn"),
    ])

    agent = AgentLoopService(provider=provider, max_iterations=25,
                             budget_config=BudgetConfig(max_input_tokens=500_000))
    result = await agent.run("How does auth work?", "/tmp/workspace")

    assert "auth/router.py" in result.answer
    assert result.tool_calls_made == 1
    assert result.budget_summary["total_input_tokens"] > 0
```

MockProvider 允许在不调用真实 API 的情况下测试 Agent Loop 的完整逻辑：工具调用、结果注入、迭代控制、预算信号、证据验证。

### 19.3 工作流引擎测试

工作流测试使用**真实的配置文件**（`config/workflows/*.yaml`、`config/agents/*.md`），只 mock AI Provider：

```python
def test_workflow_pr_review():
    workflow = load_workflow("workflows/pr_review.yaml")
    engine = WorkflowEngine(provider=MockProvider([...]))

    context = {
        "file_paths": ["src/auth/router.py", "src/auth/jwt.py"],
        "changed_lines": 150,
        "workspace_path": str(tmp_path),
    }
    result = await engine.run(workflow, context)

    # 认证文件应该触发 security 路由
    assert "security" in result["_active_routes"]
    assert "_stage_results" in result
```

---

## 20. 常见开发任务

### 20.1 添加一个新的 Agent

1. 在 `config/agents/` 下创建 `.md` 文件：

```markdown
---
name: performance
type: explorer
model_role: explorer

tools:
  core: true
  extra:
    - get_callees
    - trace_variable
    - git_log

budget_weight: 1.0

trigger:
  always: false
---

## 性能分析策略

1. 用 grep 搜索常见性能问题模式（N+1 查询、无索引、全表扫描）
2. 用 get_callees 分析热路径的调用深度
3. 用 trace_variable 追踪数据量大的变量流向
...
```

2. 在工作流 YAML 中引用它：

```yaml
# config/workflows/pr_review.yaml
routes:
  performance:           # 新增路由
    file_patterns:
      - "query|select|fetch|load|bulk"
    pipeline:
      - stage: explore
        agents: [agents/performance.md]
```

3. 运行测试确认工作流加载正常：

```bash
pytest -k "test_workflow" -v
```

### 20.2 添加一个新的代码工具

1. 在 `code_tools/tools.py` 实现工具函数：

```python
def _run_find_todo(workspace: str, params: dict) -> ToolResult:
    """搜索 TODO/FIXME 注释"""
    pattern = params.get("pattern", "TODO|FIXME|HACK|XXX")
    ws = Path(workspace)
    results = []
    for f in ws.rglob("*.py"):
        if ".git" in str(f) or "node_modules" in str(f):
            continue
        for i, line in enumerate(f.read_text(errors="ignore").splitlines(), 1):
            if re.search(pattern, line, re.IGNORECASE):
                results.append({"file": str(f.relative_to(ws)), "line": i, "content": line.strip()})
    return ToolResult(success=True, data=results)
```

2. 在 `execute_tool()` 调度器中注册：

```python
TOOL_REGISTRY = {
    ...
    "find_todo": _run_find_todo,
}
```

3. 在 `schemas.py` 的 `TOOL_DEFINITIONS` 中添加 JSON Schema（LLM 看到的工具描述）：

```python
{
    "name": "find_todo",
    "description": "Search for TODO/FIXME comments in the codebase",
    "input_schema": {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Regex pattern (default: TODO|FIXME|HACK|XXX)"}
        }
    }
}
```

4. 在 `output_policy.py` 中添加截断策略：

```python
"find_todo": Policy(max_results=30),
```

5. 在 `tests/test_code_tools.py` 中添加测试。

### 20.3 添加一个新的 AI 提供商

1. 继承 `AIProvider`：

```python
# ai_provider/my_provider.py
class MyProvider(AIProvider):
    def chat_with_tools(self, messages, tools, system="") -> ToolUseResponse:
        # 将 Bedrock 格式的 messages 转换为 My API 格式
        api_messages = _convert_messages(messages)
        api_tools = _convert_tools(tools)

        resp = my_api.chat(messages=api_messages, tools=api_tools, system=system)

        # 转换回统一格式
        return ToolUseResponse(
            text=resp.text,
            tool_calls=[ToolCall(id=tc.id, name=tc.name, input=tc.args) for tc in resp.tool_calls],
            stop_reason=resp.finish_reason,
            usage=TokenUsage(input_tokens=resp.usage.prompt, output_tokens=resp.usage.completion),
        )
```

2. 在 `ProviderResolver` 中注册：

```python
# ai_provider/resolver.py
def _configured_providers(self) -> list[tuple[str, AIProvider]]:
    ...
    if self._config.ai_providers.my_provider.api_key:
        yield "my_provider", MyProvider(self._config.ai_providers.my_provider)
```

### 20.4 修改某个 agent 的工具集或人格

直接编辑 `config/agents/*.md`，不需要改 Python 代码：

```markdown
---
name: security
tools: [grep, read_file, find_references, get_callers, trace_variable, db_schema]
limits:
  max_iterations: 25
---

## 安全审查策略
- 新增：检查 SQLAlchemy raw SQL 拼接
- ...
```

修改后重启后端即可生效（agent 在每次 Brain 启动时从文件加载）。

### 20.5 调试 Agent Loop

最快的调试方式是用同步接口并看完整日志：

```python
import asyncio
import logging
logging.basicConfig(level=logging.DEBUG)

from app.agent_loop.service import AgentLoopService
from app.agent_loop.budget import BudgetConfig
from app.ai_provider.claude_direct import ClaudeDirectProvider

provider = ClaudeDirectProvider(api_key="sk-ant-...")
agent = AgentLoopService(
    provider=provider,
    max_iterations=5,
    budget_config=BudgetConfig(max_input_tokens=50_000),
)

result = asyncio.run(agent.run(
    query="how does authentication work?",
    workspace_path="/path/to/your/repo"
))

print(result.answer)
print(f"工具调用次数: {result.tool_calls_made}")
print(f"Token 使用: {result.budget_summary}")
```

---

## 21. 部署说明

### 21.1 系统依赖

```bash
# 必需
git >= 2.15    # worktree 支持
ripgrep (rg)   # grep 工具用的底层搜索引擎

# 可选
ast-grep       # ast_search 工具（结构化 AST 查询）
docker         # 运行 Langfuse（自托管可观测性）
```

### 21.2 目录布局

运行时需要以下目录可写：

```
/var/conductor/workspaces/
├── repos/       # bare git 克隆（每个房间一个）
└── worktrees/   # 工作目录（每个房间一个）
```

每个活跃房间约占用仓库大小的 2-3 倍磁盘空间。

### 21.3 配置文件优先级

```python
# config.py — 配置文件搜索顺序
_SEARCH_DIRS = [
    Path("config/"),           # 当前工作目录下的 config/
    Path("../config/"),        # 父目录下的 config/
    Path.home() / ".conductor/",  # 用户 Home 目录
]
```

### 21.4 Dockerfile 示例

```dockerfile
FROM python:3.12-slim

RUN apt-get update && apt-get install -y \
    git \
    ripgrep \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY backend/requirements.txt .
RUN pip install -r requirements.txt

COPY backend/ .
COPY config/ ./config/

RUN mkdir -p /var/conductor/workspaces/repos /var/conductor/workspaces/worktrees

ENV GIT_WORKSPACE_ROOT=/var/conductor/workspaces

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

> 所有持久化数据（审计日志、TODO、文件元数据）存储在 PostgreSQL 中。

### 21.5 Docker 组件网络（本地开发）

本地 Docker Compose 使用三个 compose 文件，共享同一个 `conductor-net` Docker 网络：

```
docker/docker-compose.data.yaml   → Postgres (conductor-postgres:5432)
                                    Redis    (conductor-redis:6379)
docker/docker-compose.app.yaml    → Backend  (使用容器名访问 data 层)
docker/docker-compose.langfuse.yaml → Langfuse (使用容器名访问 Postgres)
```

**启动命令：**

```bash
make data-up       # 先启动 Postgres + Redis
make langfuse-up   # 启动 Langfuse（共用同一个 Postgres）
make app-up        # 启动后端（连接到同一个网络）

# 或者一键启动全栈
make docker-up
```

**注意（WSL2 场景）：** `host.docker.internal` 在某些 WSL2 配置下无法从容器内解析。所有 compose 文件已改为使用容器名（`conductor-postgres`、`conductor-redis`）代替 `host.docker.internal`，通过共享 `conductor-net` 网络互相通信。

### 21.6 健康检查与监控

```bash
# 基础存活检查
GET /health
# → {"status": "ok"}

# Prometheus 指标（最小化）
GET /metrics
# → conducator_up 1

# AI 提供商状态
GET /ai/status
# → {"active_model": "...", "available_providers": [...]}
```

### 21.7 云部署环境变量（ECS / K8s）/ Cloud Deployment Environment Variables

Docker 镜像打包了 `config/conductor.secrets.yaml`（含本地 dev 默认值）。
部署到云时，通过环境变量覆盖任意 secret — **环境变量优先于 YAML 文件**。

The Docker image ships with `config/conductor.secrets.yaml` (dev defaults baked in).
For cloud deployment, set environment variables to override any secret —
**env vars take priority over YAML values**.

**配置方式 / How to configure：** 在 ECS Task Definition / K8s Secret 中设置所需变量即可。
未设置的变量自动 fallback 到 YAML 中的 dev 默认值。

Set the variables in your ECS Task Definition or K8s Secret.
Variables not set will fall back to the dev defaults in the YAML file.

#### 完整变量列表 / Full Variable Reference

| 环境变量 | 对应 secret | 必须 |
|---------|-----------|------|
| **AI Providers** | | |
| `CONDUCTOR_AWS_ACCESS_KEY_ID` | ai_providers.aws_bedrock.access_key_id | Bedrock 时必须 |
| `CONDUCTOR_AWS_SECRET_ACCESS_KEY` | ai_providers.aws_bedrock.secret_access_key | Bedrock 时必须 |
| `CONDUCTOR_AWS_SESSION_TOKEN` | ai_providers.aws_bedrock.session_token | 临时凭证时设置 |
| `CONDUCTOR_AWS_REGION` | ai_providers.aws_bedrock.region | 默认 us-east-1 |
| `CONDUCTOR_ANTHROPIC_API_KEY` | ai_providers.anthropic.api_key | Anthropic Direct 时必须 |
| `CONDUCTOR_OPENAI_API_KEY` | ai_providers.openai.api_key | OpenAI 时必须 |
| `CONDUCTOR_ALIBABA_API_KEY` | ai_providers.alibaba.api_key | DashScope 时必须 |
| `CONDUCTOR_ALIBABA_BASE_URL` | ai_providers.alibaba.base_url | 默认新加坡区 |
| `CONDUCTOR_MOONSHOT_API_KEY` | ai_providers.moonshot.api_key | Moonshot 时必须 |
| `CONDUCTOR_MOONSHOT_BASE_URL` | ai_providers.moonshot.base_url | 默认 api.moonshot.ai |
| **Database** | | |
| `CONDUCTOR_POSTGRES_USER` | postgres.user | 默认 conductor |
| `CONDUCTOR_POSTGRES_PASSWORD` | postgres.password | 默认 conductor |
| `DATABASE_URL` | 完整连接 URL（覆盖 host/port/db 配置） | 二选一 |
| **Integrations** | | |
| `CONDUCTOR_JIRA_CLIENT_ID` | jira.client_id | Jira 集成时必须 |
| `CONDUCTOR_JIRA_CLIENT_SECRET` | jira.client_secret | Jira 集成时必须 |
| `CONDUCTOR_GOOGLE_CLIENT_ID` | google_sso.client_id | Google SSO 时必须 |
| `CONDUCTOR_GOOGLE_CLIENT_SECRET` | google_sso.client_secret | Google SSO 时必须 |
| `CONDUCTOR_NGROK_AUTHTOKEN` | ngrok.authtoken | Ngrok 时必须 |
| **Observability** | | |
| `LANGFUSE_PUBLIC_KEY` | langfuse.public_key | Langfuse 时必须 |
| `LANGFUSE_SECRET_KEY` | langfuse.secret_key | Langfuse 时必须 |
| `LANGFUSE_HOST` | langfuse.host（settings.yaml 中） | 默认 localhost:3001 |

#### ECS Task Definition 最小示例

```json
{
  "containerDefinitions": [{
    "name": "conductor-backend",
    "image": "your-ecr/conductor-backend:latest",
    "environment": [
      { "name": "CONDUCTOR_AWS_REGION", "value": "eu-west-2" },
      { "name": "CONDUCTOR_POSTGRES_PASSWORD", "value": "prod-password" },
      { "name": "CONDUCTOR_JIRA_CLIENT_ID", "value": "..." },
      { "name": "CONDUCTOR_JIRA_CLIENT_SECRET", "value": "..." },
      { "name": "LANGFUSE_PUBLIC_KEY", "value": "pk-lf-prod-..." },
      { "name": "LANGFUSE_SECRET_KEY", "value": "sk-lf-prod-..." },
      { "name": "LANGFUSE_HOST", "value": "https://langfuse.your-domain.com" }
    ],
    "secrets": [
      {
        "name": "CONDUCTOR_AWS_ACCESS_KEY_ID",
        "valueFrom": "arn:aws:secretsmanager:eu-west-2:...:conductor/aws-key-id"
      },
      {
        "name": "CONDUCTOR_AWS_SECRET_ACCESS_KEY",
        "valueFrom": "arn:aws:secretsmanager:eu-west-2:...:conductor/aws-secret"
      }
    ]
  }]
}
```

> **Tip / 提示：** 对于敏感值（API keys），推荐使用 ECS `secrets` 字段从
> AWS Secrets Manager 或 SSM Parameter Store 注入，而不是明文 `environment`。
>
> For sensitive values (API keys), prefer the ECS `secrets` field to inject from
> AWS Secrets Manager or SSM Parameter Store rather than plaintext `environment`.

---

## PR 检查清单

提交 PR 之前：

- [ ] `pytest` 通过（0 failures）
- [ ] `npm test` 通过（extension 测试）
- [ ] 新增代码有测试覆盖
- [ ] 如果修改了工作流配置：确认 `pytest -k "workflow"` 通过
- [ ] 如果修改了代码工具 schema：`make update-contracts` 并提交生成文件
- [ ] `make test-parity` 通过（Python ↔ TypeScript 工具一致性验证）
- [ ] `CLAUDE.md` 更新（如引入新模式或新模块）
- [ ] `ROADMAP.md` 更新（如完成路线图条目）
- [ ] 无硬编码的 API Key 或密码

---

*有问题找我们，或者直接看 `CLAUDE.md` 里的架构图。* 🚀
