# Conductor

[English](#english) | [中文](#中文)

---

<a name="english"></a>
## English

Conductor is a VS Code collaboration extension plus a FastAPI backend for team chat, Live Share session flow, file sharing, and AI-assisted decision/code workflows.

### Current Capabilities

- VS Code WebView collaboration panel with FSM-driven session lifecycle:
  - `Idle`
  - `BackendDisconnected` (join-only mode)
  - `ReadyToHost`
  - `Hosting`
  - `Joining`
  - `Joined`
- Live Share host/join flow with conflict checks before starting a new host session; End Chat auto-closes the active Live Share session
- Real-time WebSocket chat with:
  - reconnect recovery (`since`)
  - typing indicators
  - read receipts
  - message deduplication
  - paginated history
- File upload/download (20MB limit, extension-host upload proxy, duplicate detection, retry logic)
- Code snippet sharing + editor navigation
- Change review workflow:
  - `POST /generate-changes` (MockAgent)
  - policy check (`POST /policy/evaluate-auto-apply`)
  - per-change diff preview
  - sequential apply/skip
  - audit logging (`POST /audit/log-apply`)
- AI provider workflow:
  - provider health/status (`GET /ai/status`)
  - four-stage summary pipeline (`POST /ai/summarize`): classification, targeted summary, code relevance scoring, item extraction
  - code prompt generation (`POST /ai/code-prompt`)
  - selective code prompt generation (`POST /ai/code-prompt/selective`)
  - AI message posting to room (`POST /chat/{room_id}/ai-message`)

### Implemented vs Not Fully Wired

Implemented end-to-end:
- Session FSM + host/join UX (End Chat auto-closes Live Share)
- Chat/file/snippet workflow (file upload with duplicate detection and retry logic)
- AI summarize + code-prompt generation in extension UI

Still limited:
- `POST /generate-changes` is deterministic MockAgent output (not LLM edits)
- Backend supports `POST /ai/code-prompt/selective`, extension currently calls legacy `POST /ai/code-prompt`

### Architecture (High Level)

```text
VS Code Extension (TypeScript)
  ├─ WebView (chat.html)
  ├─ SessionService / PermissionsService
  ├─ ConductorStateMachine + Controller
  └─ DiffPreviewService
             │
             │ REST + WebSocket
             ▼
Backend (FastAPI)
  ├─ /ws/chat/{room_id} + /chat/*
  ├─ /ai/* (status, summarize, code prompt)
  ├─ /auth/* (AWS SSO + Google OAuth login)
  ├─ /generate-changes
  ├─ /policy/*
  ├─ /audit/*
  └─ /files/*
```

### Role Models (Important)

1. Local extension role (`aiCollab.role`): `lead` / `member`
- Controls extension UI feature access.

2. Backend session role (WebSocket assigned): `host` / `guest`
- Backend is authoritative for sensitive actions (for example, ending a session).

### Project Structure

```text
.
├─ backend/
│  ├─ app/
│  │  ├─ chat/
│  │  ├─ ai_provider/
│  │  ├─ agent/
│  │  ├─ auth/
│  │  ├─ policy/
│  │  ├─ audit/
│  │  └─ files/
│  └─ tests/
├─ extension/
│  ├─ src/
│  └─ media/
├─ docs/
│  ├─ ARCHITECTURE.md
│  └─ GUIDE.md
├─ config/
│  ├─ conductor.secrets.yaml.example
│  └─ conductor.settings.yaml.example
├─ shared/
│  └─ changeset.schema.json
└─ TESTING.md
```

### Quick Start

1. Install dependencies:

```bash
make setup
```

2. Start backend:

```bash
make run-backend
```

- Swagger: `http://localhost:8000/docs`
- ReDoc: `http://localhost:8000/redoc`

3. Start extension development:

```bash
cd extension
npm run compile
```

Then debug in VS Code:
- Preferred: open repo root and press `F5`, choose `Run VS Code Extension (extension/)`.
- Alternative: open `extension/` folder directly and press `F5`.

4. Package extension:

```bash
cd extension
npx @vscode/vsce package
```

Generates `ai-collab-0.0.1.vsix`.

### Docs

- Backend guide: `backend/README.md`
- Extension guide: `extension/README.md`
- Architecture details: `docs/ARCHITECTURE.md`
- Testing guide: `TESTING.md`

---

<a name="中文"></a>
## 中文

Conductor 是一个 VS Code 协作扩展 + FastAPI 后端，提供团队聊天、Live Share 会话流程、文件共享，以及 AI 决策/代码协作流程。

### 当前能力

- 基于状态机的会话生命周期：
  - `Idle`
  - `BackendDisconnected`（仅加入模式）
  - `ReadyToHost`
  - `Hosting`
  - `Joining`
  - `Joined`
- Live Share 主持/加入流程，启动新会话前会做冲突检查；结束会话时自动关闭 Live Share
- WebSocket 实时聊天：
  - 断线恢复（`since`）
  - 输入状态
  - 已读回执
  - 消息去重
  - 历史分页
- 文件上传/下载（20MB 上限，上传由 extension host 代理，重复文件检测，失败重试）
- 代码片段分享与编辑器定位跳转
- 变更审查流程：
  - `POST /generate-changes`（MockAgent）
  - 策略评估（`POST /policy/evaluate-auto-apply`）
  - 单条 Diff 预览
  - 顺序应用/跳过
  - 审计日志（`POST /audit/log-apply`）
- AI 流程：
  - Provider 状态（`GET /ai/status`）
  - 四阶段摘要（`POST /ai/summarize`）：分类、定向摘要、代码相关性评分、条目提取
  - 代码提示词生成（`POST /ai/code-prompt`）
  - 选择性代码提示词生成（`POST /ai/code-prompt/selective`）
  - AI 消息入房间（`POST /chat/{room_id}/ai-message`）

### 已实现与未完全接入

已实现：
- 会话状态机 + Host/Guest 交互（结束会话自动关闭 Live Share）
- 聊天/文件/代码片段流程（文件上传含重复检测与失败重试）
- 扩展端 AI 摘要与代码提示词流程

仍有限制：
- `POST /generate-changes` 仍是确定性 MockAgent，不是 LLM 实时改码
- 后端已支持 `POST /ai/code-prompt/selective`，扩展目前仍调用旧的 `POST /ai/code-prompt`

### 架构概览

```text
VS Code Extension (TypeScript)
  ├─ WebView (chat.html)
  ├─ SessionService / PermissionsService
  ├─ ConductorStateMachine + Controller
  └─ DiffPreviewService
             │
             │ REST + WebSocket
             ▼
Backend (FastAPI)
  ├─ /ws/chat/{room_id} + /chat/*
  ├─ /ai/*（status/summarize/code-prompt）
  ├─ /auth/*（AWS SSO + Google OAuth 登录）
  ├─ /generate-changes
  ├─ /policy/*
  ├─ /audit/*
  └─ /files/*
```

### 角色模型（重要）

1. 扩展本地角色（`aiCollab.role`）：`lead` / `member`
- 控制扩展 UI 功能入口。

2. 后端会话角色（WebSocket 连接后分配）：`host` / `guest`
- 敏感操作（如结束会话）以后端判定为准。

### 快速开始

1. 安装依赖：

```bash
make setup
```

2. 启动后端：

```bash
make run-backend
```

- Swagger: `http://localhost:8000/docs`
- ReDoc: `http://localhost:8000/redoc`

3. 启动扩展开发：

```bash
cd extension
npm run compile
```

然后在 VS Code 调试：
- 推荐：打开仓库根目录，按 `F5`，选择 `Run VS Code Extension (extension/)`
- 备选：直接打开 `extension/` 后按 `F5`

4. 打包扩展：

```bash
cd extension
npx @vscode/vsce package
```

会生成 `ai-collab-0.0.1.vsix`。
