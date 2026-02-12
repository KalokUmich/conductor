# Testing Guide

[English](#english) | [中文](#中文)

---

<a name="english"></a>
## English

This guide describes how to test Conductor backend and extension with current repository setup.

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
- `104` tests

Current breakdown:
- `tests/test_main.py`: 1
- `tests/test_chat.py`: 8
- `tests/test_mock_agent.py`: 26
- `tests/test_auto_apply_policy.py`: 28
- `tests/test_audit.py`: 14
- `tests/test_style_loader.py`: 14
- `tests/test_summary.py`: 13

Run specific modules:

```bash
cd backend
../.venv/bin/pytest tests/test_chat.py -v
../.venv/bin/pytest tests/test_mock_agent.py -v
../.venv/bin/pytest tests/test_auto_apply_policy.py -v
../.venv/bin/pytest tests/test_audit.py -v
../.venv/bin/pytest tests/test_summary.py -v
```

### 3. Extension Service Tests

Compile first:

```bash
cd extension
npm install
npm run compile
```

Run existing test files:

```bash
cd extension
node --test out/tests/conductorStateMachine.test.js
node --test out/tests/conductorController.test.js
node --test out/tests/backendHealthCheck.test.js
```

Notes:
- These tests target service logic, not full VS Code UI automation.
- `package.json` currently does not include a default `npm test` script.

### 4. Manual E2E Checklist

1. Start backend:

```bash
make run-backend
```

2. Start extension dev host:
- Open `extension/` in VS Code
- Press `F5`

3. Host flow validation:
- Panel state transitions to `ReadyToHost`
- Click `Start Session`
- Verify invite link generation and copy behavior

4. Guest flow validation:
- Paste invite URL in another VS Code instance
- Verify join success and chat connectivity

5. Chat/file/snippet validation:
- Send text messages
- Upload and download files
- Share code snippet and navigate to source location

6. AI review flow validation:
- Trigger `Generate Changes`
- Verify sequential diff preview
- Apply change(s)
- Confirm audit log endpoint is called

7. Session termination validation:
- Host ends session
- Guests receive `session_ended`
- Room files are deleted on backend

### 5. Quick API Smoke Commands

Health check:

```bash
curl http://localhost:8000/health
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
- Extension UI seems stale:
  - run `npm run compile` again, restart debug host
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
- `104`

当前分布：
- `tests/test_main.py`: 1
- `tests/test_chat.py`: 8
- `tests/test_mock_agent.py`: 26
- `tests/test_auto_apply_policy.py`: 28
- `tests/test_audit.py`: 14
- `tests/test_style_loader.py`: 14
- `tests/test_summary.py`: 13

运行指定模块：

```bash
cd backend
../.venv/bin/pytest tests/test_chat.py -v
../.venv/bin/pytest tests/test_mock_agent.py -v
../.venv/bin/pytest tests/test_auto_apply_policy.py -v
../.venv/bin/pytest tests/test_audit.py -v
../.venv/bin/pytest tests/test_summary.py -v
```

### 3. 扩展服务测试

先编译：

```bash
cd extension
npm install
npm run compile
```

运行现有测试文件：

```bash
cd extension
node --test out/tests/conductorStateMachine.test.js
node --test out/tests/conductorController.test.js
node --test out/tests/backendHealthCheck.test.js
```

说明：
- 这些测试主要覆盖服务逻辑，不是完整 VS Code UI 自动化。
- `package.json` 当前没有默认 `npm test` 脚本。

### 4. 手动 E2E 检查清单

1. 启动后端：

```bash
make run-backend
```

2. 启动扩展开发主机：
- 在 VS Code 打开 `extension/`
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

6. AI 审查流验证：
- 触发 `Generate Changes`
- 验证逐条 Diff 预览
- 应用变更
- 确认调用审计日志接口

7. 会话结束验证：
- Host 结束会话
- Guest 收到 `session_ended`
- 后端删除该房间上传文件

### 5. 快速 API 冒烟命令

健康检查：

```bash
curl http://localhost:8000/health
```

生成变更：

```bash
curl -X POST http://localhost:8000/generate-changes \
  -H "Content-Type: application/json" \
  -d '{"instruction":"Generate mock changes"}'
```

策略评估：

```bash
curl -X POST http://localhost:8000/policy/evaluate-auto-apply \
  -H "Content-Type: application/json" \
  -d '{"change_set":{"changes":[{"id":"1","file":"a.py","type":"create_file","content":"print(1)\n"}],"summary":"demo"}}'
```

### 6. 常见问题排查

- 后端不可达：
  - 确认 `make run-backend` 正在运行
  - 检查扩展配置 `aiCollab.backendUrl`
- 扩展 UI 内容旧：
  - 重新执行 `npm run compile` 并重启调试主机
- 文件上传失败：
  - 确认后端 `/files/upload/{room_id}` 可达
  - 确认文件大小不超过 20MB
