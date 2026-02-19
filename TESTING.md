# Testing Guide

[English](#english) | [中文](#中文)

---

<a name="english"></a>
## English

This guide describes how to test Conductor backend and extension with the current repository setup.

### 1. Test Scope

- Backend automated tests (pytest)
- Extension service tests (Node test runner)
- Manual E2E workflow checks

### 2. Backend Tests

Prerequisite (from repo root):

```bash
make setup-backend
```

Run all backend tests:

```bash
cd backend
../.venv/bin/pytest tests -v
```

Collect-only (verify current count):

```bash
cd backend
../.venv/bin/pytest tests --collect-only -q
```

Current collected count:
- `368`

Current breakdown:
- `tests/test_ai_provider.py`: 131
- `tests/test_prompt_builder.py`: 64
- `tests/test_auth.py`: 38
- `tests/test_auto_apply_policy.py`: 28
- `tests/test_chat.py`: 26
- `tests/test_mock_agent.py`: 26
- `tests/test_style_loader.py`: 22
- `tests/test_room_settings.py`: 18
- `tests/test_audit.py`: 14
- `tests/test_main.py`: 1

Run specific modules:

```bash
cd backend
../.venv/bin/pytest tests/test_ai_provider.py -v
../.venv/bin/pytest tests/test_prompt_builder.py -v
../.venv/bin/pytest tests/test_auth.py -v
../.venv/bin/pytest tests/test_chat.py -v
../.venv/bin/pytest tests/test_mock_agent.py -v
../.venv/bin/pytest tests/test_auto_apply_policy.py -v
../.venv/bin/pytest tests/test_audit.py -v
../.venv/bin/pytest tests/test_room_settings.py -v
```

### 3. Extension Tests

Compile first:

```bash
cd extension
npm install
npm run compile
```

Run tests:

```bash
cd extension
node --test out/tests/conductorStateMachine.test.js
node --test out/tests/conductorController.test.js
node --test out/tests/backendHealthCheck.test.js
node --test out/tests/aiMessageHandlers.test.js
node --test out/tests/ssoIdentityCache.test.js
```

Or run all extension tests at once:

```bash
cd extension
npm run test
```

Notes:
- These tests target service logic and API handler behavior, not full VS Code UI automation.
- Some tests start local HTTP servers; in restricted sandbox environments they can fail with socket permission errors (`EPERM`).

### 4. Manual E2E Checklist

1. Start backend:

```bash
make run-backend
```

2. Start extension dev host:
- Open repo root or `extension/` in VS Code
- Press `F5`

3. Host flow validation:
- Panel transitions to `ReadyToHost`
- Click `Start Session`
- Verify invite link generation and copy behavior

4. Guest flow validation:
- Paste invite URL in another VS Code instance
- Verify join success and chat connectivity

5. Chat/file/snippet validation:
- Send text messages
- Upload and download files
- Share code snippet and navigate to source location

6. AI workflow validation:
- Open AI config and verify `/ai/status`
- Use `Summarize Chat` (all or selected messages)
- Verify AI summary appears in room chat
- Generate coding prompt and verify posting to chat

7. Change review flow validation:
- Trigger `Generate Changes`
- Verify sequential diff preview
- Apply or skip changes
- Confirm audit log endpoint is called

8. Session termination validation:
- Host ends session
- Guests receive `session_ended`
- Active Live Share session is automatically closed
- Room files are deleted on backend

### 5. Quick API Smoke Commands

Health check:

```bash
curl http://localhost:8000/health
```

AI status:

```bash
curl http://localhost:8000/ai/status
```

AI summarize:

```bash
curl -X POST http://localhost:8000/ai/summarize \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"host","text":"Need auth","timestamp":1}]}'
```

Generate changes:

```bash
curl -X POST http://localhost:8000/generate-changes \
  -H "Content-Type: application/json" \
  -d '{"instruction":"Generate mock changes"}'
```

Policy evaluation:

```bash
curl -X POST http://localhost:8000/policy/evaluate-auto-apply \
  -H "Content-Type: application/json" \
  -d '{"change_set":{"changes":[{"id":"1","file":"a.py","type":"create_file","content":"print(1)\n"}],"summary":"demo"}}'
```

### 6. Common Troubleshooting

- Backend not reachable:
  - verify `make run-backend` is active
  - verify extension setting `aiCollab.backendUrl`
- AI summarize disabled:
  - check `summary.enabled` in `config/conductor.settings.yaml` and provider keys in `config/conductor.secrets.yaml`
  - check `/ai/status` response
- Extension UI seems stale:
  - run `npm run compile` again and restart debug host
- File upload fails:
  - verify backend `/files/upload/{room_id}` is reachable
  - verify file size <= 20MB

---

<a name="中文"></a>
## 中文

本指南说明在当前仓库下如何测试 Conductor 后端与扩展。

### 1. 测试范围

- 后端自动化测试（pytest）
- 扩展服务层测试（Node test runner）
- 手动端到端流程验证

### 2. 后端测试

前置（仓库根目录）：

```bash
make setup-backend
```

运行后端全部测试：

```bash
cd backend
../.venv/bin/pytest tests -v
```

仅收集（确认当前数量）：

```bash
cd backend
../.venv/bin/pytest tests --collect-only -q
```

当前收集数量：
- `368`

当前分布：
- `tests/test_ai_provider.py`: 131
- `tests/test_prompt_builder.py`: 64
- `tests/test_auth.py`: 38
- `tests/test_auto_apply_policy.py`: 28
- `tests/test_chat.py`: 26
- `tests/test_mock_agent.py`: 26
- `tests/test_style_loader.py`: 22
- `tests/test_room_settings.py`: 18
- `tests/test_audit.py`: 14
- `tests/test_main.py`: 1

### 3. 扩展测试

先编译：

```bash
cd extension
npm install
npm run compile
```

运行测试：

```bash
cd extension
node --test out/tests/conductorStateMachine.test.js
node --test out/tests/conductorController.test.js
node --test out/tests/backendHealthCheck.test.js
node --test out/tests/aiMessageHandlers.test.js
node --test out/tests/ssoIdentityCache.test.js
```

或一次运行全部扩展测试：

```bash
cd extension
npm run test
```

说明：
- 这些测试覆盖服务逻辑和 API 处理逻辑，不是完整 VS Code UI 自动化。
- 部分测试会启动本地 HTTP 服务；在受限沙箱环境中可能出现 `EPERM` 端口权限错误。

### 4. 手动 E2E 检查清单

1. 启动后端：

```bash
make run-backend
```

2. 启动扩展开发主机：
- 在 VS Code 打开仓库根目录或 `extension/`
- 按 `F5`

3. Host 流程验证：
- 面板状态进入 `ReadyToHost`
- 点击 `Start Session`
- 验证邀请链接生成与复制

4. Guest 流程验证：
- 在另一 VS Code 实例粘贴邀请链接
- 验证加入成功与聊天连通

5. 聊天/文件/片段验证：
- 发送文本消息
- 上传并下载文件
- 发送代码片段并跳转到源代码位置

6. AI 流程验证：
- 打开 AI 配置并确认 `/ai/status`
- 执行 `Summarize Chat`（全量或选中消息）
- 确认 AI 摘要回写到聊天
- 生成代码提示词并确认回写聊天

7. 变更审查流验证：
- 触发 `Generate Changes`
- 验证逐条 Diff 预览
- 应用或跳过变更
- 确认调用审计日志接口

8. 会话结束验证：
- Host 结束会话
- Guest 收到 `session_ended`
- 自动关闭活跃的 Live Share 会话
- 后端删除该房间上传文件
