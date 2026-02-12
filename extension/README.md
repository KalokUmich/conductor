# Conductor VS Code Extension

[English](#english) | [中文](#中文)

---

<a name="english"></a>
## English

Conductor extension provides a collaboration panel in VS Code, integrating session state management, chat, file sharing, and AI change review flow.

### Current Features

- FSM-driven session lifecycle:
  - `Idle`
  - `BackendDisconnected` (join-only mode)
  - `ReadyToHost`
  - `Hosting`
  - `Joining`
  - `Joined`
- Live Share integration:
  - host starts session
  - guests join via invite
  - Live Share conflict detection before starting a new host session
- WebSocket chat features:
  - reconnection recovery (`since`)
  - typing indicators
  - read receipts
  - message deduplication
  - paginated history loading
- File features:
  - upload from WebView via extension-host proxy (CORS-safe)
  - local download via save dialog
- Code snippet collaboration:
  - extract current editor selection
  - send snippet in chat
  - navigate to snippet file/line range
- Change review workflow:
  - call `/generate-changes`
  - per-change diff preview
  - sequential apply/skip
  - optional auto-apply toggle + policy check
  - audit logging after apply

### Role Model (Important)

1. Local extension role (`aiCollab.role`): `lead` / `member`
- Controls UI-level feature access in the extension.

2. Session role assigned by backend: `host` / `guest`
- Backend role is authoritative for sensitive actions (for example, ending chat session).

### Current Boundaries

- `Create Summary` button is currently placeholder behavior in WebView.
- `/summary` exists in backend but is not fully wired to extension workflow.
- Change generation currently depends on backend MockAgent, not real LLM output.
- No default `npm test` script in `package.json`.

### Development Setup

```bash
cd extension
npm install
npm run compile
```

Then open `extension/` in VS Code and press `F5`.

### Key Settings

- `aiCollab.role`: `lead` or `member`
- `aiCollab.backendUrl`: backend base URL (default `http://localhost:8000`)
- `aiCollab.autoStartLiveShare`: config flag (current flow still expects explicit `Start Session` click)

### Project Structure

```text
extension/
├─ src/
│  ├─ extension.ts
│  ├─ services/
│  │  ├─ conductorStateMachine.ts
│  │  ├─ conductorController.ts
│  │  ├─ backendHealthCheck.ts
│  │  ├─ session.ts
│  │  ├─ permissions.ts
│  │  └─ diffPreview.ts
│  └─ tests/
├─ media/
│  ├─ chat.html
│  ├─ input.css
│  └─ tailwind.css
└─ package.json
```

### Manual Validation Flow

1. Start backend: `make run-backend`
2. Launch extension host: `F5`
3. Verify transitions: Idle -> ReadyToHost -> Hosting
4. Copy invite link and join from another instance
5. Verify chat, file upload/download, and snippet sharing
6. Verify generate/review/apply workflow

### Running Existing Extension Unit Tests

```bash
cd extension
npm run compile
node --test out/tests/conductorStateMachine.test.js
node --test out/tests/conductorController.test.js
node --test out/tests/backendHealthCheck.test.js
```

### Packaging

```bash
cd extension
npx @vscode/vsce package
```

This generates `ai-collab-0.0.1.vsix`.

---

<a name="中文"></a>
## 中文

Conductor 扩展在 VS Code 中提供协作面板，整合会话状态管理、聊天、文件共享和 AI 变更审查流程。

### 当前功能

- 基于状态机的会话生命周期：
  - `Idle`
  - `BackendDisconnected`（仅加入模式）
  - `ReadyToHost`
  - `Hosting`
  - `Joining`
  - `Joined`
- Live Share 集成：
  - Host 发起会话
  - Guest 通过邀请链接加入
  - 启动新会话前检测 Live Share 冲突
- WebSocket 聊天能力：
  - 断线恢复（`since`）
  - 输入状态
  - 已读回执
  - 消息去重
  - 历史分页加载
- 文件能力：
  - WebView 通过扩展宿主代理上传（规避 CORS）
  - 保存对话框本地下载
- 代码片段协作：
  - 提取当前编辑器选区
  - 在聊天中发送片段
  - 跳转到片段对应文件和行范围
- 变更审查流程：
  - 调用 `/generate-changes`
  - 单条变更 Diff 预览
  - 顺序应用/跳过
  - 可选 Auto Apply + 策略检查
  - 应用后写审计日志

### 角色模型（重要）

1. 扩展本地角色（`aiCollab.role`）：`lead` / `member`
- 控制扩展内 UI 级别的功能入口。

2. 后端会话角色：`host` / `guest`
- 敏感操作（如结束会话）由后端角色最终判定。

### 当前边界

- WebView 的 `Create Summary` 按钮目前是占位行为。
- 后端虽有 `/summary`，但扩展端流程尚未完整接入。
- 变更生成当前依赖 MockAgent，不是实时 LLM 结果。
- `package.json` 默认没有 `npm test` 脚本。

### 开发启动

```bash
cd extension
npm install
npm run compile
```

然后在 VS Code 中打开 `extension/` 并按 `F5`。

### 关键配置

- `aiCollab.role`：`lead` 或 `member`
- `aiCollab.backendUrl`：后端地址（默认 `http://localhost:8000`）
- `aiCollab.autoStartLiveShare`：配置项存在，但当前流程仍以用户主动点击 `Start Session` 为主

### 项目结构

```text
extension/
├─ src/
│  ├─ extension.ts
│  ├─ services/
│  │  ├─ conductorStateMachine.ts
│  │  ├─ conductorController.ts
│  │  ├─ backendHealthCheck.ts
│  │  ├─ session.ts
│  │  ├─ permissions.ts
│  │  └─ diffPreview.ts
│  └─ tests/
├─ media/
│  ├─ chat.html
│  ├─ input.css
│  └─ tailwind.css
└─ package.json
```

### 手动验证流程

1. 启动后端：`make run-backend`
2. 启动扩展开发主机：`F5`
3. 验证状态流转：Idle -> ReadyToHost -> Hosting
4. 复制邀请链接，用另一个实例加入
5. 验证聊天、文件上传下载、代码片段分享
6. 验证生成/审查/应用流程

### 运行现有扩展单测

```bash
cd extension
npm run compile
node --test out/tests/conductorStateMachine.test.js
node --test out/tests/conductorController.test.js
node --test out/tests/backendHealthCheck.test.js
```

### 打包

```bash
cd extension
npx @vscode/vsce package
```

会生成 `ai-collab-0.0.1.vsix`。
