# Conductor Architecture

[English](#english) | [中文](#中文)

---

<a name="english"></a>
## English

This document describes the architecture that is currently implemented in the repository, and explicitly separates planned/not-yet-wired parts.

### 1. System Boundary

Conductor has two runtime parts:

1. VS Code extension (TypeScript)
- WebView UI
- state machine orchestration
- Live Share integration
- diff preview/apply in workspace

2. FastAPI backend (Python)
- WebSocket chat + REST APIs
- file storage
- policy checks
- audit logs
- summary extraction endpoint

```text
WebView <-> Extension Host <-> FastAPI
              |                |
              |                +-> DuckDB + local file storage
              +-> Live Share
```

### 2. Main Module Map

```text
extension/
  src/extension.ts
    ├─ SessionService
    ├─ PermissionsService
    ├─ ConductorStateMachine + ConductorController
    └─ DiffPreviewService
  media/chat.html

backend/app/
  main.py
    ├─ chat.router
    ├─ agent.router
    ├─ policy.router
    ├─ audit.router
    ├─ files.router
    └─ summary.router
```

### 3. Backend Architecture

#### Entry and Lifecycle

`backend/app/main.py`:
- registers routers
- exposes `/health` and `/public-url`
- optionally starts ngrok from config
- stops ngrok on shutdown

#### Router Responsibilities

- `chat`:
  - `GET /invite`
  - `GET /chat`
  - `GET /chat/{room_id}/history`
  - `WS /ws/chat/{room_id}`
- `agent`:
  - `POST /generate-changes` (currently MockAgent)
- `policy`:
  - `POST /policy/evaluate-auto-apply`
- `audit`:
  - `POST /audit/log-apply`
  - `GET /audit/logs`
- `files`:
  - `POST /files/upload/{room_id}`
  - `GET /files/download/{file_id}`
  - `DELETE /files/room/{room_id}`
- `summary`:
  - `POST /summary`

#### Chat Core: ConnectionManager

Current implemented responsibilities:
- room-scoped active WebSocket connections
- room user registry
- in-memory message history
- read receipt tracking
- message dedup (LRU)
- broadcast cleanup for dead sockets

#### Security Model (Chat)

- backend assigns `userId` on WebSocket connect
- first user in room becomes `host`, others become `guest`
- backend ignores forged client identity data for sensitive behavior
- only host can end a room session

### 4. Extension Architecture

#### FSM

File: `extension/src/services/conductorStateMachine.ts`

States:
- `Idle`
- `BackendDisconnected`
- `ReadyToHost`
- `Hosting`
- `Joining`
- `Joined`

Key behavior:
- join-only mode is supported via `BackendDisconnected -> JOIN_SESSION -> Joining`

#### Controller

File: `extension/src/services/conductorController.ts`

Responsibilities:
- backend health check orchestration
- FSM transition control
- invite URL parsing (`roomId`, `backendUrl`, `liveShareUrl`)

#### Session Service

File: `extension/src/services/session.ts`

Responsibilities:
- manage/persist room/session identifiers via VS Code `globalState`
- backend URL resolution (including ngrok detection)
- guest session override from invite link

#### WebView and Host Message Bridge

WebView: `extension/media/chat.html`
Host: `extension/src/extension.ts`

Implemented command bridge includes:
- session control: `startSession`, `joinSession`, `retryConnection`, `leaveSession`
- AI/review flow: `generateChanges`, `applyChanges`, `viewDiff`
- file flow: `uploadFile`, `downloadFile`
- code snippet flow: `getCodeSnippet`, `navigateToCode`
- session end confirmation: `confirmEndChat`

### 5. Data Contract

Shared schema:
- `shared/changeset.schema.json`

Core concepts:
- `ChangeSet.changes[]`
- `FileChange.type` in `create_file | replace_range`
- `replace_range` requires `range` and `content`

### 6. Runtime Sequence (Implemented)

Host start:
1. user clicks `Start Session`
2. extension runs health check
3. FSM enters `Hosting`
4. extension starts Live Share
5. invite URL generated and distributed
6. WebView connects to room WebSocket

Guest join:
1. guest pastes invite URL
2. controller parses invite
3. session service updates room/backend target
4. FSM transitions to `Joined`
5. WebView connects and starts chat

Change review:
1. extension calls `/generate-changes`
2. extension calls `/policy/evaluate-auto-apply`
3. changes queued in sequential review
4. each change diff-previewed and applied/skipped
5. applied changes logged to `/audit/log-apply`

File upload:
1. WebView sends file payload to extension host
2. extension host uploads multipart to `/files/upload/{room_id}`
3. backend returns metadata + download URL
4. WebView broadcasts file message through WebSocket

### 7. Persistence

- audit DB: `audit_logs.duckdb`
- file metadata DB: `file_metadata.duckdb`
- file binaries: `uploads/`
- chat room state is in-memory per process

### 8. Implemented vs Not Fully Wired

Implemented:
- chat/file/snippet/session/review/policy/audit workflows

Not fully wired:
- real LLM integration for change generation
- complete extension-side summary workflow (`/summary` exists, UI flow is placeholder)

---

<a name="中文"></a>
## 中文

本文档描述仓库中当前已经实现的架构，并明确区分“已实现”和“尚未完整接入”的部分。

### 1. 系统边界

Conductor 由两部分运行时组成：

1. VS Code 扩展（TypeScript）
- WebView UI
- 状态机编排
- Live Share 集成
- 工作区 Diff 预览与应用

2. FastAPI 后端（Python）
- WebSocket 聊天与 REST API
- 文件存储
- 策略评估
- 审计日志
- 摘要提取接口

```text
WebView <-> Extension Host <-> FastAPI
              |                |
              |                +-> DuckDB + 本地文件存储
              +-> Live Share
```

### 2. 主要模块图

```text
extension/
  src/extension.ts
    ├─ SessionService
    ├─ PermissionsService
    ├─ ConductorStateMachine + ConductorController
    └─ DiffPreviewService
  media/chat.html

backend/app/
  main.py
    ├─ chat.router
    ├─ agent.router
    ├─ policy.router
    ├─ audit.router
    ├─ files.router
    └─ summary.router
```

### 3. 后端架构

#### 入口与生命周期

`backend/app/main.py`：
- 注册所有 router
- 暴露 `/health` 与 `/public-url`
- 根据配置可选启动 ngrok
- 关闭时停止 ngrok

#### 路由职责

- `chat`：
  - `GET /invite`
  - `GET /chat`
  - `GET /chat/{room_id}/history`
  - `WS /ws/chat/{room_id}`
- `agent`：
  - `POST /generate-changes`（当前 MockAgent）
- `policy`：
  - `POST /policy/evaluate-auto-apply`
- `audit`：
  - `POST /audit/log-apply`
  - `GET /audit/logs`
- `files`：
  - `POST /files/upload/{room_id}`
  - `GET /files/download/{file_id}`
  - `DELETE /files/room/{room_id}`
- `summary`：
  - `POST /summary`

#### 聊天核心：ConnectionManager

当前实现职责：
- 按房间管理活跃 WebSocket 连接
- 房间用户注册
- 内存消息历史
- 已读回执记录
- 消息去重（LRU）
- 广播失败连接清理

#### 聊天安全模型

- WebSocket 连接时由后端分配 `userId`
- 房间首个用户为 `host`，其余为 `guest`
- 敏感行为不信任客户端伪造身份
- 仅 host 可结束会话

### 4. 扩展架构

#### 状态机

文件：`extension/src/services/conductorStateMachine.ts`

状态：
- `Idle`
- `BackendDisconnected`
- `ReadyToHost`
- `Hosting`
- `Joining`
- `Joined`

关键行为：
- 支持 `BackendDisconnected -> JOIN_SESSION -> Joining` 的仅加入模式

#### 控制器

文件：`extension/src/services/conductorController.ts`

职责：
- 编排后端健康检查
- 驱动状态机转换
- 解析邀请链接（`roomId`、`backendUrl`、`liveShareUrl`）

#### SessionService

文件：`extension/src/services/session.ts`

职责：
- 用 VS Code `globalState` 管理会话标识
- 解析后端地址（含 ngrok 探测）
- 根据邀请链接覆盖 guest 侧会话目标

#### WebView 与宿主消息桥

WebView：`extension/media/chat.html`
宿主：`extension/src/extension.ts`

已实现命令桥包括：
- 会话控制：`startSession`、`joinSession`、`retryConnection`、`leaveSession`
- AI/审查流：`generateChanges`、`applyChanges`、`viewDiff`
- 文件流：`uploadFile`、`downloadFile`
- 代码片段流：`getCodeSnippet`、`navigateToCode`
- 结束会话确认：`confirmEndChat`

### 5. 数据契约

共享 schema：
- `shared/changeset.schema.json`

核心约束：
- `ChangeSet.changes[]`
- `FileChange.type` 取值 `create_file | replace_range`
- `replace_range` 必须带 `range` 与 `content`

### 6. 关键运行时序（已实现）

Host 发起：
1. 点击 `Start Session`
2. 扩展做健康检查
3. FSM 进入 `Hosting`
4. 启动 Live Share
5. 生成并分发邀请链接
6. WebView 连接房间 WebSocket

Guest 加入：
1. 粘贴邀请链接
2. 控制器解析链接
3. SessionService 更新 room/backend 目标
4. FSM 转到 `Joined`
5. WebView 建连并开始聊天

变更审查：
1. 调用 `/generate-changes`
2. 调用 `/policy/evaluate-auto-apply`
3. 扩展建立顺序审查队列
4. 每条变更逐条预览并应用/跳过
5. 成功应用写入 `/audit/log-apply`

文件上传：
1. WebView 把文件负载发给扩展宿主
2. 宿主用 multipart 上传到 `/files/upload/{room_id}`
3. 后端返回元数据与下载地址
4. WebView 通过 WebSocket 广播文件消息

### 7. 持久化

- 审计库：`audit_logs.duckdb`
- 文件元数据库：`file_metadata.duckdb`
- 文件二进制目录：`uploads/`
- 聊天房间状态为进程内内存数据

### 8. 已实现 vs 未完整接入

已实现：
- 聊天、文件、代码片段、会话、审查、策略、审计流程

未完整接入：
- 真实 LLM 变更生成
- 扩展端摘要完整流程（后端 `/summary` 已存在，前端流程仍为占位）
