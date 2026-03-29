# Conductor VS Code Extension

[English](#english) | [中文](#中文)

---

<a name="english"></a>
## English

Conductor is a VS Code extension for real-time collaborative development with AI assistance. It provides a WebView-based chat sidebar, a Git-worktree-backed virtual file system (`conductor://`), workspace indexing, agentic code search, slash-command-driven `@AI` chat (`/ask`, `/pr`), workflow visualization panel, TODO management, stack trace sharing, and multi-provider SSO.

### Session Lifecycle (FSM)

The extension drives all state through a finite state machine persisted in `globalState`:

| State | Description |
|-------|-------------|
| `Idle` | No active session; backend reachable |
| `BackendDisconnected` | Backend unreachable; limited join-only mode |
| `ReadyToHost` | Backend healthy; user can start a session |
| `Hosting` | Host session active; workspace indexed |
| `Joining` | Connecting to a remote session |
| `Joined` | Guest session active |

`Hosting` and `Joined` survive extension-host restarts (e.g. when `Open Workspace` reloads VS Code).

### Features

#### Collaboration
- **`conductor://` virtual file system** — `ConductorFileSystemProvider` mounts a remote backend worktree as `conductor://{room_id}/`, making it browsable and editable in VS Code like a local folder.
- **Git Workspace wizard** (`workspacePanel.ts`) — 5-step UI to clone a remote repo (PAT + URL), select branch, create a backend worktree, and open it as a `conductor://` workspace folder.

#### Chat
- Real-time WebSocket chat (`/ws/chat/{room_id}`)
- Reconnection recovery with cursor-based history replay (`since`)
- Typing indicators, read receipts, message deduplication
- Paginated history loading

#### File Sharing
- Upload from WebView via extension-host proxy (CORS-safe, `FormData + Blob`)
- Duplicate filename detection before upload (case-insensitive)
- Retry logic (3 attempts) for upload and duplicate check
- Local download via VS Code save dialog
- Drag-and-drop gracefully degrades (sidebar WebViews intercept OS file drops)

#### Code Intelligence
- **Code snippet sharing** — Extract editor selection and send in chat; recipients can navigate back to the file and line range.
- **Agentic code explanation** — Unified into `_handleAskAI()` with optional `codeContext`. When the user clicks "Explain" on a code snippet (or sends `@AI` with an attached snippet), the backend Brain orchestrator explores the codebase and returns a contextual explanation. Progress is shown via the Brain tree indicator with a Stop button. Results are posted as collapsible AI explanation cards (green styling) in the chat sidebar.
- **Workspace search** — `conductor.searchWorkspace` command: full-text search over the active `conductor://` workspace via `POST /workspace/{room_id}/search`.
- **Stack trace parsing** — Shares stack traces in chat with resolved file paths and line anchors.

#### @AI Slash Commands
- Type `@AI /` in the chat input to open a floating command menu
- `/ask <question>` — ask a question about the codebase (runs agent loop)
- `/pr <base>...<feature>` — trigger a multi-agent PR review (e.g. `@AI /pr main...feature/auth`)
- Menu filters by prefix as you type; ↑↓ navigate, Enter/Tab select, Escape close
- Ghost text hint overlay shows the expected argument format for each command
- Backward compatible: bare `@AI xxx` and `@AI do PR ...` still work unchanged

#### Workflow Visualization
- `conductor.showWorkflow` command (graph icon in chat header) opens a side-panel
- SVG graph of the active workflow (PR Review or Code Explorer) with dark glass theme
- Nodes: explorer (violet border), judge (indigo border), classifier (diamond), group (dashed)
- Click a node to see agent details: tool list, budget, trigger conditions, prompt excerpt
- Two workflow tabs: PR Review and Code Explorer

#### AI Workflows
- Fetch provider status and switch active AI model
- Summarize all or selected chat messages (`/ai/summarize`)
- Generate coding prompt from decision summary (`/ai/code-prompt`, `/ai/code-prompt/selective`, `/ai/code-prompt/items`)
- Optionally post generated prompts back into chat
- **Workflows tab in AI Config modal** — select explorer/judge model per workflow; persists across reloads

#### Change Review
- Call `/generate-changes` to produce a `ChangeSet`
- Policy safety check via `/policy/evaluate-auto-apply`
- Per-change diff preview in VS Code's built-in diff editor
- Sequential apply / skip with audit logging

#### Workspace Indexing
- On session start, indexes the workspace into a local SQLite DB (`.conductor/`)
- Extracts AST symbols via `workspaceIndexer`
- Incremental re-scan on branch change; per-file reindex on file save
- Indexed symbols are used by the backend agentic code tools (find_symbol, file_outline, dependency graph)

#### TODO Management
- Full CRUD: create, list, update, delete TODOs via `/todos/{room_id}`
- `scanWorkspaceTodos` — scans source files for `TODO:`, `FIXME:` comments and surfaces them in the sidebar

#### SSO Authentication
- AWS SSO device authorization flow (`/auth/sso/start` → `/auth/sso/poll`)
- Google OAuth device authorization flow (`/auth/google/start` → `/auth/google/poll`)
- Identity cached in `globalState` with TTL; stale identities cleared on reload

### Role Model

1. **Extension role** (`aiCollab.role`): `lead` / `member` — controls UI-level feature visibility.
2. **Session role** (assigned by backend): `host` / `guest` — authoritative for sensitive actions such as ending a session.

### Key Settings

| Setting | Default | Description |
|---------|---------|-------------|
| `aiCollab.role` | `lead` | `lead` or `member` |
| `aiCollab.backendUrl` | `http://localhost:8000` | Backend base URL |

### Project Structure

```text
extension/
├─ src/
│  ├─ extension.ts                        # Activation, command registration (incl. conductor.showWorkflow)
│  ├─ services/
│  │  ├─ conductorStateMachine.ts         # FSM states and transitions
│  │  ├─ conductorController.ts           # FSM driver (start/join/stop)
│  │  ├─ conductorFileSystemProvider.ts   # conductor:// virtual FS
│  │  ├─ workspacePanel.ts               # Git workspace 5-step wizard
│  │  ├─ workspaceClient.ts              # /workspace/ HTTP client
│  │  ├─ workspaceIndexer.ts             # AST symbol extraction + incremental indexing
│  │  ├─ workflowPanel.ts               # WorkflowPanel singleton — workflow visualization WebView
│  │  ├─ (explainWithContextPipeline.ts removed — merged into extension.ts _handleAskAI with codeContext)
│  │  ├─ lspResolver.ts                 # VS Code LSP definition + references
│  │  ├─ relevanceRanker.ts             # Hybrid structural + semantic relevance scoring
│  │  ├─ contextPlanGenerator.ts        # Deduplicated read-file operation planner
│  │  ├─ xmlPromptAssembler.ts          # Structured XML prompt builder for LLM
│  │  ├─ todoScanner.ts                  # Workspace TODO/FIXME scanner
│  │  ├─ stackTraceParser.ts             # Stack trace parsing and path resolution
│  │  ├─ diffPreview.ts                  # Diff preview + apply for ChangeSets
│  │  ├─ session.ts                      # Room/session persistence
│  │  ├─ permissions.ts                  # Role-based access control
│  │  ├─ backendHealthCheck.ts           # Backend liveness probe
│  │  ├─ ssoIdentityCache.ts             # SSO identity with TTL
│  │  ├─ languageDetector.ts             # Workspace language detection
│  │  └─ conductorDb.ts                  # SQLite DB wrapper (.conductor/)
│  └─ tests/                             # Node test runner tests
├─ media/
│  ├─ chat.html                          # WebView HTML — @AI slash commands (/ask /pr), AI Config modal
│  ├─ workflow.html                      # Workflow visualization WebView — SVG graph + agent details
│  ├─ input.css
│  └─ tailwind.css
└─ package.json
```

### Development Setup

```bash
cd extension
npm install
npm run compile       # one-time build
npm run watch         # watch mode
```

### Debugging (F5)

Two ways to launch the Extension Development Host:

1. Open the repo root (`conducator/`) and press `F5` → select `Run VS Code Extension (extension/)`.
2. Open `extension/` directly and press `F5`.

In VS Code Remote mode the Extension Development Host may open with no folder. Use the root launch config which opens a fallback folder.

### Running Tests

```bash
cd extension
npm run compile
npm run test                              # runs all out/tests/*.test.js
```

Individual test files:

```bash
node --test out/tests/conductorStateMachine.test.js
node --test out/tests/conductorController.test.js
node --test out/tests/backendHealthCheck.test.js
node --test out/tests/conductorFileSystemProvider.test.js
node --test out/tests/workspaceIndexer.test.js
node --test out/tests/ragClient.test.js
node --test out/tests/embeddingQueue.test.js
```

> Some tests spin up local HTTP servers. In restricted sandbox environments they may fail with `EPERM` socket errors.

### Manual Validation Flow

1. `make run-backend`
2. `F5` → verify `Idle → ReadyToHost → Hosting`
3. Copy invite link; join from another VS Code window
4. Test chat, file upload/download, snippet sharing
5. Open the Git Workspace wizard; clone a repo; verify `conductor://` folder mounts
6. Run `conductor.searchWorkspace` and verify results
7. Test AI summary + code prompt workflow
8. Test TODO create / update / delete

### Packaging

```bash
cd extension
npx @vscode/vsce package
```

Generates `ai-collab-0.0.1.vsix`.

---

<a name="中文"></a>
## 中文

Conductor 是一个 VS Code 扩展，提供基于 WebView 的协作侧边栏、Git worktree 虚拟文件系统（`conductor://`）、工作区索引、智能代码搜索、斜杠命令驱动的 `@AI` 聊天（`/ask`、`/pr`）、工作流可视化面板、TODO 管理、堆栈追踪共享及多 Provider SSO。

### 会话生命周期（状态机）

所有会话状态通过 FSM 驱动，持久化在 `globalState`：

| 状态 | 说明 |
|------|------|
| `Idle` | 无活跃会话；后端可连接 |
| `BackendDisconnected` | 后端不可达；仅限加入模式 |
| `ReadyToHost` | 后端健康；可发起会话 |
| `Hosting` | Host 会话进行中；工作区已建索引 |
| `Joining` | 正在连接远端会话 |
| `Joined` | Guest 会话进行中 |

`Hosting` 和 `Joined` 状态在扩展宿主重启（如 `Open Workspace` 触发 VS Code 重载）后可自动恢复。

### 功能列表

#### 协作
- **`conductor://` 虚拟文件系统** — `ConductorFileSystemProvider` 将远端后端 worktree 挂载为 `conductor://{room_id}/`，可在 VS Code 中像本地文件夹一样浏览和编辑。
- **Git 工作区向导**（`workspacePanel.ts`）— 5 步 UI，通过 PAT + URL 克隆远端仓库，选择分支，创建后端 worktree，并作为 `conductor://` 工作区文件夹打开。

#### 聊天
- 实时 WebSocket 聊天（`/ws/chat/{room_id}`）
- 断线恢复（cursor-based 历史重放，`since` 参数）
- 输入状态、已读回执、消息去重
- 历史分页加载

#### 文件共享
- WebView 通过扩展宿主代理上传（规避 CORS，使用 `FormData + Blob`）
- 上传前重复文件检测（大小写不敏感）
- 上传和重复检测均有失败重试（最多 3 次）
- 本地保存下载（VS Code 保存对话框）
- VS Code 侧边栏 WebView 中拖拽优雅降级

#### 代码智能
- **代码片段共享** — 提取当前编辑器选区并发送到聊天；接收方可跳转至对应文件和行范围。
- **Agentic 代码解释** — 已合并到 `extension.ts` 的 `_handleAskAI()` 方法中，通过 `codeContext` 参数区分普通 @AI 查询和代码解释。点击代码片段的 "Explain" 按钮或发送带代码附件的 `@AI` 消息，均通过 Brain 智能体探索代码库并返回解释。
- **工作区搜索** — `conductor.searchWorkspace` 命令：通过 `POST /workspace/{room_id}/search` 对活跃 `conductor://` 工作区进行全文搜索。
- **堆栈追踪解析** — 共享堆栈追踪，并解析文件路径和行号定位。

#### AI 流程
- 获取 Provider 状态并切换活动 AI 模型
- 摘要全部或选中聊天消息（`/ai/summarize`）
- 生成代码提示词（`/ai/code-prompt`、`/ai/code-prompt/selective`、`/ai/code-prompt/items`）
- 可选：将生成的提示词写回聊天

#### 变更审查
- 调用 `/generate-changes` 生成 `ChangeSet`
- 通过 `/policy/evaluate-auto-apply` 评估安全性
- VS Code 内置 Diff 编辑器逐条预览
- 顺序应用/跳过，应用后写审计日志

#### 工作区索引
- 会话启动时将工作区索引写入本地 SQLite DB（`.conductor/`）
- 通过 `workspaceIndexer` 提取 AST 符号
- 分支切换时硬重置索引；文件保存时增量更新
- 索引符号供后端 Agentic 代码工具使用（find_symbol、file_outline、依赖图）

#### TODO 管理
- 完整 CRUD：通过 `/todos/{room_id}` 创建、列出、更新、删除 TODO
- `scanWorkspaceTodos` — 扫描源文件中的 `TODO:`、`FIXME:` 注释，在侧边栏展示

#### SSO 认证
- AWS SSO 设备授权流程（`/auth/sso/start` → `/auth/sso/poll`）
- Google OAuth 设备授权流程（`/auth/google/start` → `/auth/google/poll`）
- 身份缓存在 `globalState`（带 TTL），重载时清除过期缓存

### 角色模型

1. **扩展角色**（`aiCollab.role`）：`lead` / `member` — 控制 UI 功能入口。
2. **会话角色**（后端分配）：`host` / `guest` — 敏感操作（如结束会话）以后端判定为准。

### 关键配置

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `aiCollab.role` | `lead` | `lead` 或 `member` |
| `aiCollab.backendUrl` | `http://localhost:8000` | 后端地址 |

### 开发启动

```bash
cd extension
npm install
npm run compile       # 一次性构建
npm run watch         # 监听模式
```

### 调试（F5）

两种方式：

1. 打开仓库根目录（`conducator/`）按 `F5` → 选择 `Run VS Code Extension (extension/)`。
2. 直接打开 `extension/` 后按 `F5`。

VS Code Remote 模式下 Extension Development Host 可能无工作区，使用根目录 fallback 调试配置。

### 运行测试

```bash
cd extension
npm run compile
npm run test                              # 运行所有 out/tests/*.test.js
```

单独运行：

```bash
node --test out/tests/conductorStateMachine.test.js
node --test out/tests/conductorController.test.js
node --test out/tests/conductorFileSystemProvider.test.js
node --test out/tests/workspaceIndexer.test.js
node --test out/tests/ragClient.test.js
```

> 部分测试启动本地 HTTP 服务，在受限沙箱中可能因端口权限（`EPERM`）失败。

### 打包

```bash
cd extension
npx @vscode/vsce package
```

生成 `ai-collab-0.0.1.vsix`。
