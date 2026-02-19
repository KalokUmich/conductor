# Conductor VS Code Extension

[English](#english) | [中文](#中文)

---

<a name="english"></a>
## English

Conductor extension provides a collaboration panel in VS Code, integrating session state management, chat, file sharing, and AI-assisted review/summarization workflows.

### Current Features

- FSM-driven session lifecycle:
  - `Idle`
  - `BackendDisconnected` (join-only mode)
  - `ReadyToHost`
  - `Hosting`
  - `Joining`
  - `Joined`
- Live Share integration:
  - Host starts session
  - Guests join from invite URL
  - Existing Live Share conflict check before starting a new host session
  - End Chat auto-closes the active Live Share session
- WebSocket chat features:
  - reconnection recovery (`since`)
  - typing indicators
  - read receipts
  - message deduplication
  - paginated history loading
- File features:
  - upload from WebView via extension-host proxy (CORS-safe, FormData + Blob)
  - duplicate file detection before upload (case-insensitive filename match)
  - retry logic (3 attempts) for both upload and duplicate check requests
  - local download via save dialog
  - drag-and-drop gracefully degrades in VS Code (sidebar WebViews intercept OS file drops)
- Code snippet collaboration:
  - extract current editor selection
  - send snippet in chat
  - navigate to snippet file/line range
- Change review workflow:
  - call `/generate-changes`
  - policy check via `/policy/evaluate-auto-apply`
  - per-change diff preview
  - sequential apply/skip
  - audit logging after apply
- AI workflow in WebView:
  - fetch provider status (`/ai/status`)
  - summarize all or selected messages (`/ai/summarize`)
  - post summary into chat (`/chat/{room_id}/ai-message`)
  - generate code prompt (`/ai/code-prompt`) and optionally post back into chat

### Role Model (Important)

1. Local extension role (`aiCollab.role`): `lead` / `member`
- Controls UI-level feature access in the extension.

2. Session role assigned by backend: `host` / `guest`
- Backend role is authoritative for sensitive actions (for example, ending a session).

### Current Boundaries

- `/generate-changes` currently depends on backend MockAgent output.
- Backend supports selective prompt API (`/ai/code-prompt/selective`), extension currently calls `/ai/code-prompt`.
- AI message posting currently sends `model_name=claude_bedrock` as a fixed label (TODO in code).

### Development Setup

```bash
cd extension
npm install
npm run compile
```

### Debugging (F5)

You can debug in two ways:

1. Open repo root (`conducator/`) and press `F5`.
- Use launch config: `Run VS Code Extension (extension/)`.

2. Open `extension/` folder directly and press `F5`.

If you are in VS Code Remote mode, Extension Development Host may open with no folder due remote limitations. In that case use the root launch config that opens a fallback folder.

### Key Settings

- `aiCollab.role`: `lead` or `member`
- `aiCollab.backendUrl`: backend base URL (default `http://localhost:8000`)
- `aiCollab.autoStartLiveShare`: config exists but current flow still expects explicit `Start Session`

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
│  │  ├─ diffPreview.ts
│  │  ├─ languageDetector.ts
│  │  └─ ssoIdentityCache.ts
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
3. Verify transitions: `Idle -> ReadyToHost -> Hosting`
4. Copy invite link and join from another instance
5. Verify chat, file upload/download, and snippet sharing
6. Verify generate/review/apply workflow
7. Verify AI summary + code prompt workflow in chat sidebar

### Running Existing Extension Tests

```bash
cd extension
npm run compile
npm run test                # Runs all out/tests/*.test.js
```

Or run individual test files:

```bash
node --test out/tests/conductorStateMachine.test.js
node --test out/tests/conductorController.test.js
node --test out/tests/backendHealthCheck.test.js
node --test out/tests/aiMessageHandlers.test.js
node --test out/tests/ssoIdentityCache.test.js
```

Note: some tests spin up local HTTP servers. In restricted sandbox environments they may fail with socket permission errors (`EPERM`).

### Packaging

```bash
cd extension
npx @vscode/vsce package
```

This generates `ai-collab-0.0.1.vsix`.

---

<a name="中文"></a>
## 中文

Conductor 扩展在 VS Code 中提供协作面板，整合会话状态机、聊天、文件共享与 AI 审查/摘要流程。

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
  - 启动新会话前检查已有 Live Share 冲突
  - 结束会话时自动关闭 Live Share
- WebSocket 聊天能力：
  - 断线恢复（`since`）
  - 输入状态
  - 已读回执
  - 消息去重
  - 历史分页加载
- 文件能力：
  - WebView 通过扩展宿主代理上传（规避 CORS，使用 FormData + Blob）
  - 上传前重复文件检测（大小写不敏感文件名匹配）
  - 上传和重复检测均有失败重试机制（最多 3 次）
  - 本地保存下载
  - VS Code 中拖拽上传优雅降级（侧边栏 WebView 拦截系统拖拽事件）
- 代码片段协作：
  - 提取当前编辑器选区
  - 在聊天中发送片段
  - 跳转到片段文件/行范围
- 变更审查流程：
  - 调用 `/generate-changes`
  - 调用 `/policy/evaluate-auto-apply`
  - 单条 Diff 预览
  - 顺序应用/跳过
  - 应用后写审计日志
- WebView 中 AI 流程：
  - 获取 provider 状态（`/ai/status`）
  - 摘要全部或选中消息（`/ai/summarize`）
  - 将摘要写回聊天（`/chat/{room_id}/ai-message`）
  - 生成代码提示词（`/ai/code-prompt`）并可回写聊天

### 角色模型（重要）

1. 扩展本地角色（`aiCollab.role`）：`lead` / `member`
- 控制扩展内 UI 功能入口。

2. 后端会话角色：`host` / `guest`
- 敏感操作（如结束会话）以后端判定为准。

### 当前边界

- `/generate-changes` 仍依赖后端 MockAgent。
- 后端已有 selective 提示词接口（`/ai/code-prompt/selective`），扩展目前仍调用 `/ai/code-prompt`。
- AI 消息写回聊天时 `model_name` 目前固定为 `claude_bedrock`（代码中有 TODO）。

### 开发启动

```bash
cd extension
npm install
npm run compile
```

### 调试（F5）

两种方式：

1. 打开仓库根目录（`conducator/`）按 `F5`。
- 选择 `Run VS Code Extension (extension/)`。

2. 直接打开 `extension/` 后按 `F5`。

如果你在 VS Code Remote 模式下调试，Extension Development Host 可能出现 `NO FOLDER OPENED`（Remote 限制）；可使用根目录下的 fallback 调试配置。

### 关键配置

- `aiCollab.role`：`lead` 或 `member`
- `aiCollab.backendUrl`：后端地址（默认 `http://localhost:8000`）
- `aiCollab.autoStartLiveShare`：配置存在，但当前流程仍以用户手动点击 `Start Session` 为主

### 运行现有扩展测试

```bash
cd extension
npm run compile
npm run test                # 运行所有 out/tests/*.test.js
```

或单独运行某个测试：

```bash
node --test out/tests/conductorStateMachine.test.js
node --test out/tests/conductorController.test.js
node --test out/tests/backendHealthCheck.test.js
node --test out/tests/aiMessageHandlers.test.js
node --test out/tests/ssoIdentityCache.test.js
```

说明：部分测试会启动本地 HTTP 服务，在受限沙箱环境中可能因为端口权限（`EPERM`）失败。

### 打包

```bash
cd extension
npx @vscode/vsce package
```

会生成 `ai-collab-0.0.1.vsix`。
