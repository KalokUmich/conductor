# Conductor

[English](#english) | [中文](#中文)

---

<a name="english"></a>
## English

Conductor is a VS Code collaboration extension plus a FastAPI backend for team chat, Live Share session flow, and AI-assisted code-change review.

### Current Capabilities

- VS Code WebView collaboration panel
- Live Share host/join workflow
- Real-time WebSocket chat with:
  - reconnection recovery (`since`)
  - read receipts
  - paginated history
- File upload/download (20MB max)
- Code snippet sharing and editor navigation
- MockAgent-based change generation + diff preview + sequential apply
- Auto-apply policy evaluation
- DuckDB audit logging

### Implemented vs Not Fully Connected

Implemented:
- Session FSM (`Idle`, `BackendDisconnected`, `ReadyToHost`, `Hosting`, `Joining`, `Joined`)
- Join-only mode when local backend is unavailable
- Invite page (`GET /invite`) and guest chat page (`GET /chat`)
- Audit logging, file lifecycle management, policy checks

Available backend API but not fully wired in extension UI:
- `POST /summary` exists in backend
- `Create Summary` button in WebView is currently a placeholder action

AI generation status:
- `POST /generate-changes` currently uses deterministic `MockAgent`
- Real LLM integration is not connected yet

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
  ├─ /generate-changes
  ├─ /policy/evaluate-auto-apply
  ├─ /audit/*
  ├─ /files/*
  └─ /summary
```

### Two Role Models (Important)

1. Local extension role (`aiCollab.role`): `lead` / `member`
- Controls UI feature visibility in VS Code extension.

2. Backend session role (assigned on WebSocket connect): `host` / `guest`
- Backend is the source of truth for sensitive operations (for example, ending a session).

### Project Structure

```text
.
├─ backend/
│  ├─ app/
│  │  ├─ chat/
│  │  ├─ agent/
│  │  ├─ policy/
│  │  ├─ audit/
│  │  ├─ files/
│  │  └─ summary/
│  └─ tests/
├─ extension/
│  ├─ src/
│  └─ media/
├─ docs/
│  └─ ARCHITECTURE.md
├─ config/
│  └─ conductor.yaml.example
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

3. Start extension development host:

```bash
cd extension
npm run compile
```

Open `extension/` in VS Code and press `F5`.

### Docs

- Backend guide: `backend/README.md`
- Extension guide: `extension/README.md`
- Architecture details: `docs/ARCHITECTURE.md`
- Testing guide: `TESTING.md`

---

<a name="中文"></a>
## 中文

Conductor 是一个 VS Code 协作扩展 + FastAPI 后端，提供团队聊天、Live Share 会话流程和 AI 代码变更审查能力。

### 当前能力

- VS Code WebView 协作面板
- Live Share 主持/加入流程
- WebSocket 实时聊天，支持：
  - 断线恢复（`since`）
  - 已读回执
  - 历史分页
- 文件上传/下载（最大 20MB）
- 代码片段分享与编辑器定位跳转
- 基于 MockAgent 的变更生成 + Diff 预览 + 顺序应用
- Auto-Apply 策略评估
- DuckDB 审计日志

### 已实现与未完全接入

已实现：
- 会话状态机（`Idle`、`BackendDisconnected`、`ReadyToHost`、`Hosting`、`Joining`、`Joined`）
- 本地后端不可用时的 Join Only 模式
- 邀请页（`GET /invite`）与访客聊天页（`GET /chat`）
- 审计、文件生命周期、策略检查

后端已提供但前端尚未完全接入：
- 后端有 `POST /summary`
- WebView 的 `Create Summary` 按钮当前仍是占位行为

AI 生成现状：
- `POST /generate-changes` 当前使用确定性 `MockAgent`
- 真实 LLM 尚未接入

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
  ├─ /generate-changes
  ├─ /policy/evaluate-auto-apply
  ├─ /audit/*
  ├─ /files/*
  └─ /summary
```

### 两套角色模型（重要）

1. 扩展本地角色（`aiCollab.role`）：`lead` / `member`
- 决定扩展 UI 功能可见性。

2. 后端会话角色（WebSocket 连接后分配）：`host` / `guest`
- 后端是敏感操作（如结束会话）的权限判定来源。

### 目录结构

```text
.
├─ backend/
│  ├─ app/
│  │  ├─ chat/
│  │  ├─ agent/
│  │  ├─ policy/
│  │  ├─ audit/
│  │  ├─ files/
│  │  └─ summary/
│  └─ tests/
├─ extension/
│  ├─ src/
│  └─ media/
├─ docs/
│  └─ ARCHITECTURE.md
├─ config/
│  └─ conductor.yaml.example
├─ shared/
│  └─ changeset.schema.json
└─ TESTING.md
```

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

3. 启动扩展开发调试：

```bash
cd extension
npm run compile
```

在 VS Code 打开 `extension/` 后按 `F5`。

### 文档索引

- 后端说明：`backend/README.md`
- 扩展说明：`extension/README.md`
- 架构文档：`docs/ARCHITECTURE.md`
- 测试文档：`TESTING.md`

## License

MIT, see `LICENSE`.
