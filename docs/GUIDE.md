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
├── chat.html                  — 聊天 WebView，@AI 斜杠命令菜单
│                                在线模式房间列表，renderMessageByType，Highlight.js 代码高亮
├── workflow.html              — 工作流可视化面板（SVG 图）
├── workflowPanel.ts           — 面板控制器
└── services/
    ├── localToolDispatcher.ts — 三级工具派发：子进程 → AST → 原生 TS
    ├── astToolRunner.ts       — 6 个 AST 工具（基于 web-tree-sitter）
    ├── treeSitterService.ts   — web-tree-sitter WASM 封装（8 种语言）
    ├── complexToolRunner.ts   — 6 个复杂工具（compressed_view、trace_variable 等）
    └── chatLocalStore.ts      — 本地消息缓存（VS Code globalState）

FastAPI Backend
├── workflow/          — 配置驱动的多 Agent 工作流引擎  ← 核心新增
├── agent_loop/        — LLM Agent Loop（24 个代码工具）
├── code_review/       — PR 多 Agent 评审管线
├── ai_provider/       — 三提供商抽象层（Bedrock / Anthropic / OpenAI）
├── git_workspace/     — Git 裸仓库 + Worktree 管理
├── chat/              — WebSocket 聊天 + Redis 热缓存 + Postgres 持久化
├── browser/           — Playwright Chromium 浏览工具（browse_url / search_web / screenshot）
├── code_tools/        — 24 个代码智能工具实现 + Python CLI 入口
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

# 列出可用工作流
curl http://localhost:8000/api/workflows
# → [{"name": "pr-review", ...}, {"name": "code-explorer", ...}]

# 代码问答（同步接口，适合调试）
curl -X POST http://localhost:8000/api/context/query \
  -H "Content-Type: application/json" \
  -d '{"query": "how does authentication work?", "workspace_path": "/path/to/repo"}'
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

**第一步：Extension 解析命令**（`extension/media/chat.html`）

用户在 textarea 里输入 `@AI /ask 认证逻辑在哪里？` 并按 Enter。

```javascript
// chat.html — sendMessage()
function sendMessage() {
    const text = textarea.value;
    // 匹配 "@AI /ask xxx" 或 "@AI /pr xxx"
    const slashMatch = text.match(/@AI\s+\/(\w+)\s+(.*)/is);
    if (slashMatch) {
        const cmd = SLASH_COMMANDS.find(c => c.name === '/' + slashMatch[1]);
        query = cmd ? cmd.transform(slashMatch[2]) : text;
    }
    // "/ask" 的 transform 是直接透传，"/pr" 的 transform 会加 "do PR" 前缀
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

**第三步：Backend 路由到 Agent Loop**（`backend/app/agent_loop/router.py`）

```python
@router.post("/api/context/query/stream")
async def query_stream(req: QueryRequest, request: Request):
    workspace_path = _resolve_workspace(req, request)  # 从 room_id 或直接路径解析

    # 加载工作流配置（code-explorer workflow）
    workflow = load_workflow("workflows/code_explorer.yaml")
    engine = WorkflowEngine(
        provider=request.app.state.agent_provider,
        explorer_provider=request.app.state.explorer_provider,
    )

    async def generate():
        context = {"query": req.query, "workspace_path": workspace_path}
        async for event in engine.run_stream(workflow, context):
            yield f"data: {json.dumps(event.__dict__)}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")
```

**第四步：WorkflowEngine 分类并路由**（`backend/app/workflow/engine.py`）

```python
async def run_stream(self, workflow, context):
    # 1. 分类：用 keyword_pattern 匹配查询文本
    engine = ClassifierEngine(workflow)
    result = engine.classify({"query_text": context["query"]})
    # "认证逻辑" → 命中 entry_point_discovery 路由（含 "where is" 关键词）

    # 2. 路由：first_match 模式，执行最匹配的那条路由
    route = workflow.routes[result.best_route]

    # 3. 执行路由的 pipeline（每个 stage 里的 agent）
    async for event in self._run_pipeline(route.pipeline, workflow, context, route.name):
        yield event
```

**第五步：AgentLoopService 迭代探索**（`backend/app/agent_loop/service.py`）

```python
async def run_stream(self, query, workspace_path):
    messages = [{"role": "user", "content": query}]
    system_prompt = build_system_prompt(query_type, workspace_layout)

    for i in range(self.max_iterations):
        # LLM 决定下一步用哪个工具
        response = provider.chat_with_tools(messages, tools=active_tools, system=system_prompt)

        if response.stop_reason == "end_turn":
            # LLM 认为已经有足够信息，准备给出答案
            if evidence_ok(response.text, tool_calls_made):
                return AgentResult(answer=response.text, ...)
            else:
                # 证据不足，注入反馈让 LLM 继续调查
                messages.append(feedback_message("需要具体文件引用"))
                continue

        # 执行 LLM 选择的工具
        for tool_call in response.tool_calls:
            result = execute_tool(tool_call.name, workspace_path, tool_call.input)
            messages.append(tool_result_block(tool_call.id, result))

        # 检查预算
        budget_signal = budget_controller.check(token_usage)
        if budget_signal == FORCE_CONCLUDE:
            messages.append(budget_note("请立即给出答案"))
```

**整条链路：**
```
浏览器输入 → chat.html 解析 → extension.ts SSE 请求 →
agent_loop/router.py → WorkflowEngine.run_stream() →
ClassifierEngine.classify() → _run_pipeline() →
AgentLoopService.run_stream() → LLM ↔ execute_tool() 循环 →
SSE 事件流回 → chat.html 显示实时进度
```

---

### 3.2 场景 B：用户输入 `@AI /pr main...feature/auth`

**第一步：Extension 解析并发送**（同上，`/pr` 的 transform 加前缀）

```javascript
{ name: '/pr', transform: (args) => `do PR ${args}` }
// "@AI /pr main...feature/auth" → query = "do PR main...feature/auth"
```

**第二步：WorkflowEngine 识别 PR 命令**（`code_explorer.yaml`）

Code Explorer 工作流有一条 `code_review` 路由，`text_patterns` 匹配 `"review|pr review|do pr"`:

```yaml
# config/workflows/code_explorer.yaml
routes:
  code_review:
    text_patterns:
      - "review|pr review|pull request|do pr|check the pr"
    delegate: workflows/pr_review.yaml   # 委托给 PR Review 工作流
```

引擎识别到 `delegate`，加载 `pr_review.yaml` 并重新运行。

**第三步：PR Review 工作流并行派发**（`pr_review.yaml`，`parallel_all_matching` 模式）

```python
# engine.py — _run_parallel_all_matching()
async def _run_parallel_all_matching(self, workflow, classify_result, context):
    # 根据 git diff 的文件路径，用 risk_pattern 分类器识别涉及哪些维度
    # 文件包含 auth/... → security: HIGH; 有 try/except → reliability: MEDIUM

    active_routes = ["correctness", "security", "test_coverage"]  # always_run 的也激活

    # 全部并行运行
    await asyncio.gather(*[_run_one_route(rn) for rn in active_routes])

    # 所有路由结束后，顺序执行 post_pipeline
    # post_pipeline: 仲裁 (arbitrator.md) → 综合 (review_synthesizer.md)
    for stage in workflow.post_pipeline:
        await self._run_stage(stage, context)
```

**第四步：每个 Agent 独立运行 AgentLoopService**（同场景 A 的第五步，但每个 Agent 有自己的工具集和指令）

**整条链路：**
```
"do PR main...feature/auth" →
WorkflowEngine → ClassifierEngine(risk_pattern, file paths) →
asyncio.gather(correctness_agent, security_agent, test_coverage_agent) 并行 →
各 Agent 独立跑 AgentLoopService →
post_pipeline: arbitrator_agent → synthesizer_agent →
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
│   ├── workflow/                  # ★ 配置驱动的工作流引擎（核心新模块）
│   │   ├── models.py              # Pydantic 模型：WorkflowConfig、AgentConfig、StageConfig
│   │   ├── loader.py              # 加载 YAML 工作流 + Markdown Agent 文件
│   │   ├── classifier_engine.py   # 分类器：risk_pattern（PR Review）+ keyword_pattern（代码问答）
│   │   ├── engine.py              # WorkflowEngine：first_match + parallel_all_matching
│   │   ├── mermaid.py             # 从配置自动生成 Mermaid 流程图
│   │   ├── router.py              # /api/workflows/ 接口（5 个端点）
│   │   └── observability.py       # Langfuse @observe 装饰器（禁用时零开销）
│   │
│   ├── agent_loop/                # LLM Agent 循环引擎
│   │   ├── service.py             # AgentLoopService — LLM 循环 + 工具派发
│   │   ├── budget.py              # BudgetController — token 预算三级信号
│   │   ├── trace.py               # SessionTrace — JSON 追踪（离线分析用）
│   │   ├── query_classifier.py    # QueryClassifier — 关键词 + 可选 LLM 分类
│   │   ├── evidence.py            # EvidenceEvaluator — 答案质量门控
│   │   ├── prompts.py             # 三层 System Prompt 构建
│   │   └── router.py              # POST /api/context/query (+ /stream)
│   │
│   ├── code_tools/                # 24 个代码智能工具
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
│   ├── workflows/
│   │   ├── pr_review.yaml         # PR 评审工作流：6 条路由，parallel_all_matching
│   │   └── code_explorer.yaml     # 代码问答工作流：9 条路由，first_match
│   ├── agents/                    # 18 个 Agent 定义文件（YAML 头部 + Markdown 正文）
│   │   ├── security.md            # PR 探索 Agent：认证/注入/XSS
│   │   ├── correctness.md         # PR 探索 Agent：逻辑/状态/持久化
│   │   ├── ... (15 more)
│   └── prompts/
│       ├── review_base.md         # 共享评审提示词
│       └── explorer_base.md       # 共享探索提示词（CORE_IDENTITY）
│
├── requirements.txt
└── tests/                         # 1300+ 测试
    ├── conftest.py                # 中央 stub（cocoindex、litellm 等）
    ├── test_code_tools.py         # 98 个：24 工具 + 多语言
    ├── test_agent_loop.py         # 47 个：循环 + 三层 Prompt + 完整性检查
    ├── test_budget_controller.py  # 20 个：预算信号
    ├── test_query_classifier.py   # 26 个：分类 + 动态工具集
    ├── test_compressed_tools.py   # 24 个：压缩视图工具
    ├── test_evidence.py           # 14 个：证据门控
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

## 6. 配置驱动的工作流引擎

这是最近加入的最重要的新模块，也是理解 Conductor 核心编排逻辑的关键。

### 6.1 核心抽象

每个任务都遵循同一个模式：

```
输入 → 分类器 → 路由 → Agent(s) → 聚合 → 输出
```

只有两种路由模式，覆盖所有场景：

| 模式 | 行为 | 用于 |
|------|------|------|
| `first_match` | 分类器选最匹配的路由，执行该路由的 pipeline | 代码问答（Code Explorer）|
| `parallel_all_matching` | 所有匹配的路由并行执行，再顺序执行 `post_pipeline` | PR 评审 |

### 6.2 工作流配置文件解析

**代码问答工作流** (`code_explorer.yaml`，`first_match` 模式)：

```yaml
name: code-explorer
route_mode: first_match

# 预算配置：整个工作流的 token 总量和迭代上限
budget:
  base_tokens: 500_000
  base_iterations: 25

# 分类器类型：keyword_pattern = 匹配查询文本中的关键词
dispatch:
  classifier:
    type: keyword_pattern

routes:
  # 路由名 → 触发条件 + pipeline
  business_flow_tracing:
    text_patterns:
      - "flow|process|trace|how does|what happens"
    pipeline:
      - stage: explore
        parallel: true          # 两个 Agent 并行
        agents:
          - agents/explore_implementation.md
          - agents/explore_usage.md
      - stage: synthesize       # 等上面完成后执行
        agents:
          - agents/explore_synthesizer.md

  root_cause_analysis:
    text_patterns:
      - "bug|error|fail|why|root cause"
    pipeline:
      - stage: investigate
        agents: [agents/explore_root_cause.md]

  # code_review 路由委托给另一个工作流
  code_review:
    text_patterns:
      - "review|pr review|do pr"
    delegate: workflows/pr_review.yaml
```

**PR 评审工作流** (`pr_review.yaml`，`parallel_all_matching` 模式)：

```yaml
name: pr-review
route_mode: parallel_all_matching

budget:
  base_tokens: 800_000
  base_iterations: 40
  # PR 大小乘数：小 PR 用 0.5×，大 PR 用 2.0×
  size_multiplier:
    small:  { max_lines: 500,   factor: 0.5 }
    large:  { max_lines: 5000,  factor: 1.5 }

# 分类器类型：risk_pattern = 匹配 git diff 的文件路径
dispatch:
  classifier:
    type: risk_pattern

routes:
  security:
    file_patterns:
      - "auth|login|session|token|jwt|oauth"
      - "password|secret|credential|api.?key"
    pipeline:
      - stage: explore
        agents: [agents/security.md]

  test_coverage:
    file_patterns: []   # 空 = 由 agent 的 always_run 控制
    pipeline:
      - stage: explore
        agents: [agents/test_coverage.md]

# 所有路由并行完成后，顺序执行这些阶段
post_pipeline:
  - stage: arbitrate
    agents: [agents/arbitrator.md]    # 仲裁严重程度
  - stage: synthesize
    agents: [agents/review_synthesizer.md]  # 生成最终报告
```

### 6.3 Agent 定义文件解析

每个 Agent 是一个 Markdown 文件，YAML 头部是元数据，正文是指令：

```markdown
---
name: security
type: explorer          # explorer = 用 AgentLoopService；judge = 单次 LLM 调用
model_role: explorer    # explorer（轻量模型）或 strong（强模型）

tools:
  core: true            # 包含工作流的 core_tools（grep、read_file 等）
  extra:                # 这个 Agent 额外使用的工具
    - find_references
    - get_callers
    - trace_variable    # 数据流追踪，安全 Agent 必备

budget_weight: 1.0      # 相对预算权重（1.0 = 标准份额）

trigger:
  always: false         # true = 无论分类结果如何都激活（test_coverage 使用 true）

input: [diff_spec, workspace_path]
output: findings
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

### 6.4 WorkflowEngine 执行流程

```python
# workflow/engine.py

class WorkflowEngine:
    async def run_stream(self, workflow: WorkflowConfig, context: dict):
        # Step 1: 分类
        engine = ClassifierEngine(workflow)
        result = engine.classify(context)

        # Step 2: 按模式派发
        if workflow.route_mode == "first_match":
            # 选最匹配的路由
            route = workflow.routes[result.best_route]
            if route.delegate:
                # 委托：加载另一个工作流重新运行
                delegate_wf = load_workflow(route.delegate)
                async for event in self.run_stream(delegate_wf, context):
                    yield event
            else:
                async for event in self._run_pipeline(route.pipeline, ...):
                    yield event

        elif workflow.route_mode == "parallel_all_matching":
            # 所有匹配的路由并发运行
            await asyncio.gather(*[_run_one_route(rn) for rn in active_routes])
            # post_pipeline 顺序运行
            for stage in workflow.post_pipeline:
                await self._run_stage(stage, context)

    async def _run_agent(self, agent: AgentConfig, ...):
        if agent.type == "explorer":
            # 启动完整的 AgentLoopService（多轮 LLM + 工具调用）
            svc = AgentLoopService(provider=self._resolve_provider(agent.model_role), ...)
            return await svc.run(query=query, workspace_path=workspace_path)
        elif agent.type == "judge":
            # 单次 LLM 调用，不使用工具
            return await provider.call_model(prompt=prompt, max_tokens=agent.max_tokens)
```

### 6.5 分类器引擎

`classifier_engine.py` 实现两种分类器：

**keyword_pattern**（代码问答）：

```python
# 对每条路由的 text_patterns 计分
# 查询 "why does auth fail" → root_cause_analysis (bug|fail 匹配) 得分最高
for route_name, route in workflow.routes.items():
    score = sum(
        len(re.findall(pattern, query_text, re.IGNORECASE))
        for pattern in route.text_patterns
    )
    scores[route_name] = score
best_route = max(scores, key=scores.get)
```

**risk_pattern**（PR 评审）：

```python
# 对每个维度，统计 diff 中有多少文件路径匹配其 file_patterns
for route_name, route in workflow.routes.items():
    matched_files = [
        f for f in changed_file_paths
        if any(re.search(pat, f, re.IGNORECASE) for pat in route.file_patterns)
    ]
    level = _level_from_count(len(matched_files), route.thresholds)
    result.matched_routes[route_name] = level   # "low" | "medium" | "high" | "critical"
```

### 6.6 工作流 API 接口

```bash
# 列出所有可用工作流
GET /api/workflows
# → [{"name": "pr-review", "route_mode": "parallel_all_matching", "agent_count": 7}, ...]

# 工作流详情（包含所有路由和 Agent 配置）
GET /api/workflows/pr-review

# Mermaid 流程图（可在 GitHub Markdown 中渲染）
GET /api/workflows/pr-review/mermaid

# React Flow 图（工作流可视化面板用）
GET /api/workflows/code-explorer/graph

# 更新 Explorer/Judge 模型配置
PUT /api/workflows/pr-review/models
{ "explorer": "claude-haiku-4-5", "judge": "claude-sonnet-4-6" }
```

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

### 7.2 24 个代码工具

所有工具在 `code_tools/tools.py` 中实现，通过 `execute_tool(name, workspace, params)` 统一调度。

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

### 7.5 三层系统提示词（prompts.py）

每次 LLM 调用的 system prompt 由三层组成：

```
L1: Core Identity（~30 行，始终包含）
    ├── 目标导向：理解代码行为、定位功能、追踪数据流
    ├── 如何调查：先搜索概念词汇，再深度阅读，理解领域模型
    └── 答案格式：必须包含文件:行号引用，代码块引用实际代码

L2: Strategy（仅 code_review 查询类型使用，其余为空）
    └── code_review: 结构化输出模板（发现摘要格式）

L3: Runtime Guidance（动态生成）
    ├── 当前预算信号（NORMAL / WARN_CONVERGE / FORCE_CONCLUDE）
    ├── 已访问文件数和符号数
    └── 迭代次数 / 剩余迭代次数
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
// chat.html — 用户选择在线模式时触发
function selectMode(mode) {
    if (mode === 'online') {
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
      ├── grep/git/read 等 12 个子进程工具
      ├── file_outline/find_symbol 等 6 个 AST 工具（web-tree-sitter）
      └── compressed_view/trace_variable 等 6 个复杂工具
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

Conductor 通过 Atlassian OAuth 2.0 (3LO) 授权码流程连接 Jira Cloud，让团队成员直接从 VS Code 创建和搜索 Jira issue。

### 15.1 OAuth 3LO 流程

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
JiraUriHandler.handleUri()
      → GET /api/integrations/jira/status
      → 更新 globalState（带 48h 时间戳）
      ↓
连接成功，显示已连接的 Jira 站点 URL
```

**配置（conductor.secrets.yaml）：**

```yaml
jira:
  client_id: "your-atlassian-client-id"
  client_secret: "your-atlassian-client-secret"
  redirect_uri: "https://your-backend-url/api/integrations/jira/callback"
  # 可选：静态团队列表（避免每次请求 create-meta API）
  teams:
    - id: "uuid-1234"
      name: "Platform"
```

### 15.2 后端服务 (JiraOAuthService)

`JiraOAuthService` 管理整个 OAuth 生命周期。Token 存储在**内存**中（重启后清空，用户需重新授权）：

```python
# integrations/jira/service.py — 使用示例
svc = JiraOAuthService(
    client_id="...",
    client_secret="...",
    redirect_uri="...",
    static_teams=[JiraFieldOption(id="uuid-1234", name="Platform")]
)

# 1. 生成授权 URL（带 state 防重放）
result = svc.get_authorize_url()
# → {"authorize_url": "https://auth.atlassian.com/authorize?...", "state": "abc..."}

# 2. OAuth callback 交换授权码
token_pair = await svc.exchange_code(code, state)
# → JiraTokenPair(access_token, refresh_token, cloud_id, site_url)

# 3. 查看连接状态
status = svc.get_status()
# → {"connected": True, "site_url": "https://your-org.atlassian.net"}

# 4. 创建 issue
issue = await svc.create_issue(CreateIssueRequest(
    project_key="PLAT",
    summary="Fix authentication bug in JWT refresh",
    description="Token expiry not checked before use",
    issue_type="Bug",
    priority="High",
    team="Platform",
))
# → JiraIssue(id="12345", key="PLAT-42", browse_url="https://...atlassian.net/browse/PLAT-42")
```

**API 端点：**

```bash
GET  /api/integrations/jira/authorize-url  # 获取授权 URL
GET  /api/integrations/jira/callback        # OAuth 浏览器重定向处理
POST /api/integrations/jira/callback        # Extension 直接交换 code
GET  /api/integrations/jira/status          # 当前连接状态
POST /api/integrations/jira/disconnect      # 断开连接
GET  /api/integrations/jira/projects        # 列出可访问项目
GET  /api/integrations/jira/issue-types     # 查询项目的 issue 类型
GET  /api/integrations/jira/create-meta     # 创建 issue 所需字段元数据
GET  /api/integrations/jira/search          # JQL 文本搜索（?q=...&maxResults=10）
POST /api/integrations/jira/issues          # 创建 issue
```

### 15.3 Extension 前端 (jiraAuthService.ts)

连接信息存储在 `globalState`，key 为 `conductor.jiraConnection`，有效期 48 小时：

```typescript
import { getValidJiraConnection, wrapJiraConnection, JIRA_GLOBALSTATE_KEY } from './jiraAuthService';

// 读取缓存（过期或旧格式返回 null）
const stored = context.globalState.get(JIRA_GLOBALSTATE_KEY);
const conn = getValidJiraConnection(stored);
if (conn) {
    // 连接有效，conn.siteUrl / conn.cloudId 可用
} else {
    // 需要重新授权
}

// 授权成功后写入缓存（带时间戳）
context.globalState.update(JIRA_GLOBALSTATE_KEY,
    wrapJiraConnection({ connected: true, siteUrl, cloudId }));
```

`JiraUriHandler` 注册为 VS Code URI 处理器，监听 `vscode://publisher.conductor/jira/callback`，自动处理授权码交换或刷新连接状态。

### 15.4 常见问题

| 症状 | 原因 | 解法 |
|------|------|------|
| `Jira integration is not enabled` | `jira.client_id` 未在 secrets 中配置 | 填写 `conductor.secrets.yaml` → `make app-restart` |
| 连接后立即失效 | 旧格式 globalState（无 `storedAt`）| 清除 `globalState` 后重新授权 |
| Team 字段找不到 | `customfield_10001` 因 Jira 实例不同而变化 | 先调用 `GET /create-meta` 确认 team_field_key |
| 重启后断开连接 | Token 只存内存 | 正常行为，重新点击 Connect Jira |

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
workflow: pr-review                      45.2s  $0.38
├── classify: risk_pattern               0.1ms
│   → correctness=HIGH, security=MEDIUM
├── route: correctness                   35.2s  $0.12
│   └── agent: correctness (explorer)
│       ├── llm_call (generation)         1.2s   →工具: grep
│       ├── llm_call (generation)         0.9s   →工具: read_file
│       └── ... (共 18 次工具调用)
├── route: security                      32.8s  $0.09
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
├── code_review/          代码评审质量（plants-bug 用例，12 个 requests 测试）
├── agent_quality/        Agent Loop 答案质量（基线对比）
└── tool_parity/          Python vs TypeScript 工具输出对比
```

详细文档见 `eval/README.md`。

### 18.1 代码评审评估（code_review/）

在真实开源代码库中植入已知 bug（git patch），运行完整的 `CodeReviewService` 管线，检查发现的结果是否匹配预期。

```bash
cd backend

# 运行全部 12 个用例
python ../eval/code_review/run.py --provider anthropic --model claude-sonnet-4-20250514

# 只运行特定用例（快速验证）
python ../eval/code_review/run.py --filter "requests-001" --no-judge

# 保存当前结果为基线（下次对比用）
python ../eval/code_review/run.py --save-baseline

# 黄金标准：直接运行 Claude Code CLI（质量上限）
python ../eval/code_review/run.py --gold --gold-model opus --save-baseline
```

**12 个测试用例：** 4 简单、5 中等、3 困难（基于 requests v2.31.0）。

**评分维度：** 召回率 (35%)、精确率 (20%)、严重程度准确性 (15%)、位置准确性 (10%)、修复建议 (10%)、上下文深度 (10%)。

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
pytest tests/test_query_classifier.py -v         # 查询分类器
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
| `test_code_tools.py` | 98 | 全部 24 工具 + 多语言支持 |
| `test_agent_loop.py` | 47 | Agent Loop + 三层 Prompt + 完整性检查 |
| `test_budget_controller.py` | 20 | 预算信号转换、追踪、边界情况 |
| `test_session_trace.py` | 15 | SessionTrace JSON 保存/加载 |
| `test_evidence.py` | 14 | 证据评估器质量门控 |
| `test_symbol_role.py` | 24 | 符号角色分类 + 装饰器检测 |
| `test_output_policy.py` | 19 | 每工具截断策略、预算自适应 |
| `test_query_classifier.py` | 26 | 关键词 + LLM 分类、动态工具集 |
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

### 20.4 修改工作流路由的触发条件

直接编辑 `config/workflows/*.yaml`，不需要改 Python 代码：

```yaml
# config/workflows/code_explorer.yaml
routes:
  root_cause_analysis:
    text_patterns:
      - "bug|error|fail|why|root cause|debug|crash"
      - "exception|broken|wrong|unexpected"   # 新增触发词
```

修改后重启后端即可生效（工作流在每次请求时从文件加载）。

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
