# Backend API

[English](#english) | [中文](#中文)

---

<a name="english"></a>
## English

Conductor backend is built with FastAPI and provides chat, files, AI summary/code-prompt APIs, change generation, policy checks, and audit logs.

### Quick Start

Recommended (from repo root):

```bash
make setup-backend
make run-backend
```

Manual:

```bash
cd backend
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Docs:
- Swagger: `http://localhost:8000/docs`
- ReDoc: `http://localhost:8000/redoc`

### API Overview

#### System

| Method | Path | Description |
|---|---|---|
| GET | `/health` | Health check |
| GET | `/public-url` | Current ngrok public URL (if enabled) |

#### Chat and Invite

| Method | Path | Description |
|---|---|---|
| GET | `/invite` | Invite page with Live Share CTA and embedded chat |
| GET | `/chat` | Guest chat page |
| GET | `/chat/{room_id}/history` | Cursor-based paginated chat history |
| POST | `/chat/{room_id}/ai-message` | Post AI summary/code prompt message into room |
| WS | `/ws/chat/{room_id}` | Real-time chat WebSocket |

#### AI Provider Endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/ai/status` | Summary enabled flag + active provider health status |
| POST | `/ai/summarize` | Four-stage AI summary pipeline (classification + targeted summary + code relevance + item extraction) |
| POST | `/ai/code-prompt` | Generate coding prompt from decision summary |
| POST | `/ai/code-prompt/selective` | Generate selective coding prompt from multi-type summary |
| POST | `/ai/model` | Set active AI model |
| GET | `/ai/style-templates` | List available style templates |

#### Room Settings

| Method | Path | Description |
|---|---|---|
| GET | `/rooms/{room_id}/settings` | Get room settings |
| PUT | `/rooms/{room_id}/settings` | Update room settings |

#### AI Changes and Policy

| Method | Path | Description |
|---|---|---|
| POST | `/generate-changes` | Generate ChangeSet (currently MockAgent) |
| POST | `/policy/evaluate-auto-apply` | Evaluate auto-apply safety |

#### Audit

| Method | Path | Description |
|---|---|---|
| POST | `/audit/log-apply` | Record manual/auto apply operation |
| GET | `/audit/logs` | Query audit logs (optional `room_id` filter) |

#### Files

| Method | Path | Description |
|---|---|---|
| POST | `/files/upload/{room_id}` | Upload file to a room |
| GET | `/files/download/{file_id}` | Download file |
| GET | `/files/check-duplicate/{room_id}` | Check duplicate filename (case-insensitive) |
| DELETE | `/files/room/{room_id}` | Delete all files for a room |

#### Auth (SSO)

| Method | Path | Description |
|---|---|---|
| POST | `/auth/sso/start` | Start AWS SSO device authorization flow |
| POST | `/auth/sso/poll` | Poll for AWS SSO token and resolve identity |
| POST | `/auth/google/start` | Start Google OAuth device authorization flow |
| POST | `/auth/google/poll` | Poll for Google OAuth token and resolve identity |
| GET | `/auth/providers` | List enabled auth providers |

### AI Provider Notes

- Provider selection is configured in `config/conductor.settings.yaml` under `summary`, with provider keys in `config/conductor.secrets.yaml`.
- Resolver priority: `anthropic` -> `aws_bedrock` -> `openai`.
- Only providers with non-empty keys are health-checked.
- `aws_bedrock` requires `boto3` (already in `requirements.txt`).
- `anthropic` requires `anthropic` package (optional dependency; install manually if using direct Claude API).
- `openai` requires `openai` package (optional dependency; install manually if using OpenAI API).

### WebSocket Protocol (Core)

Connection:
- `ws://<host>/ws/chat/{room_id}`

Server -> Client:
- `connected`
- `history`
- `message`
- `file`
- `code_snippet`
- `typing`
- `read_receipt`
- `user_joined` / `user_left`
- `session_ended`
- `error`

Client -> Server:
- `join`
- `message`
- `file`
- `code_snippet`
- `typing`
- `read`
- `end_session`

Security model:
- Backend assigns `userId` and role (`host` for first connection, then `guest`).
- Backend ignores forged client identity for sensitive behavior.
- Only host can end session.

### Request Examples

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
  -d '{
    "messages": [
      {"role": "host", "text": "Need auth flow with JWT", "timestamp": 1730000000},
      {"role": "engineer", "text": "Add login endpoint and middleware", "timestamp": 1730000001}
    ]
  }'
```

Generate code prompt:

```bash
curl -X POST http://localhost:8000/ai/code-prompt \
  -H "Content-Type: application/json" \
  -d '{
    "decision_summary": {
      "type": "decision_summary",
      "topic": "JWT auth",
      "problem_statement": "No secure login",
      "proposed_solution": "Implement JWT login and middleware",
      "requires_code_change": true,
      "affected_components": ["auth/login.py", "auth/middleware.py"],
      "risk_level": "medium",
      "next_steps": ["add endpoint", "add tests"]
    }
  }'
```

Generate changes (MockAgent):

```bash
curl -X POST http://localhost:8000/generate-changes \
  -H "Content-Type: application/json" \
  -d '{
    "file_path": "src/main.py",
    "instruction": "Generate mock changes",
    "file_content": "print(\"hello\")"
  }'
```

Evaluate policy:

```bash
curl -X POST http://localhost:8000/policy/evaluate-auto-apply \
  -H "Content-Type: application/json" \
  -d '{
    "change_set": {
      "changes": [{
        "id": "1",
        "file": "src/main.py",
        "type": "replace_range",
        "range": {"start": 1, "end": 2},
        "content": "print(\"updated\")\\n"
      }],
      "summary": "update"
    }
  }'
```

### Storage

- Audit DB: `audit_logs.duckdb`
- File metadata DB: `file_metadata.duckdb`
- File content: `uploads/{room_id}/...`
- Chat room state: in-memory per process

### Tests

Current backend collection count is `368`.

```bash
cd backend
../.venv/bin/pytest tests --collect-only -q
../.venv/bin/pytest tests -v
```

Breakdown:
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

### Known Limits

- `POST /generate-changes` is still MockAgent-based.
- Audit/files use local DuckDB + disk (single-node local design).

---

<a name="中文"></a>
## 中文

Conductor 后端基于 FastAPI，提供聊天、文件、AI 摘要/代码提示词接口、变更生成、策略评估与审计日志。

### 快速启动

推荐（仓库根目录执行）：

```bash
make setup-backend
make run-backend
```

手动方式：

```bash
cd backend
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

文档地址：
- Swagger: `http://localhost:8000/docs`
- ReDoc: `http://localhost:8000/redoc`

### API 总览

#### 系统

| Method | Path | 说明 |
|---|---|---|
| GET | `/health` | 健康检查 |
| GET | `/public-url` | 当前 ngrok 公网地址（如启用） |

#### 聊天与邀请

| Method | Path | 说明 |
|---|---|---|
| GET | `/invite` | 邀请页（含 Live Share 按钮与嵌入聊天） |
| GET | `/chat` | 访客聊天页 |
| GET | `/chat/{room_id}/history` | 历史消息分页 |
| POST | `/chat/{room_id}/ai-message` | 将 AI 摘要/提示词写入房间消息 |
| WS | `/ws/chat/{room_id}` | 实时聊天 WebSocket |

#### AI Provider 接口

| Method | Path | 说明 |
|---|---|---|
| GET | `/ai/status` | 摘要开关、活动 provider 与健康状态 |
| POST | `/ai/summarize` | 四阶段摘要流水线（分类 + 定向摘要 + 代码相关性 + 条目提取） |
| POST | `/ai/code-prompt` | 基于决策摘要生成代码提示词 |
| POST | `/ai/code-prompt/selective` | 基于多类型摘要生成 selective 提示词 |
| POST | `/ai/model` | 设置活动 AI 模型 |
| GET | `/ai/style-templates` | 列出可用的样式模板 |

#### 房间设置

| Method | Path | 说明 |
|---|---|---|
| GET | `/rooms/{room_id}/settings` | 获取房间设置 |
| PUT | `/rooms/{room_id}/settings` | 更新房间设置 |

#### AI 变更与策略

| Method | Path | 说明 |
|---|---|---|
| POST | `/generate-changes` | 生成 ChangeSet（当前 MockAgent） |
| POST | `/policy/evaluate-auto-apply` | 评估自动应用安全性 |

#### 审计

| Method | Path | 说明 |
|---|---|---|
| POST | `/audit/log-apply` | 记录手动/自动应用操作 |
| GET | `/audit/logs` | 查询审计日志（可按 `room_id` 过滤） |

#### 文件

| Method | Path | 说明 |
|---|---|---|
| POST | `/files/upload/{room_id}` | 向房间上传文件 |
| GET | `/files/download/{file_id}` | 下载文件 |
| GET | `/files/check-duplicate/{room_id}` | 检查文件名是否重复（大小写不敏感） |
| DELETE | `/files/room/{room_id}` | 删除房间全部文件 |

#### Auth（SSO）

| Method | Path | 说明 |
|---|---|---|
| POST | `/auth/sso/start` | 启动 AWS SSO 设备授权流程 |
| POST | `/auth/sso/poll` | 轮询 AWS SSO token 并解析身份 |
| POST | `/auth/google/start` | 启动 Google OAuth 设备授权流程 |
| POST | `/auth/google/poll` | 轮询 Google OAuth token 并解析身份 |
| GET | `/auth/providers` | 列出启用的认证提供商 |

### AI Provider 说明

- Provider 在 `config/conductor.settings.yaml` 的 `summary` 节配置，provider 密钥在 `config/conductor.secrets.yaml`。
- 解析优先级：`anthropic` -> `aws_bedrock` -> `openai`。
- 只有配置了 key 的 provider 才会做健康检查。
- `aws_bedrock` 依赖 `boto3`（已在 `requirements.txt`）。
- `anthropic` 依赖 `anthropic`（可选依赖，使用直连 Claude 时需手动安装）。
- `openai` 依赖 `openai`（可选依赖，使用 OpenAI 时需手动安装）。

### 测试

当前后端测试收集数为 `368`。

```bash
cd backend
../.venv/bin/pytest tests --collect-only -q
../.venv/bin/pytest tests -v
```

分布：
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

### 已知限制

- `POST /generate-changes` 仍是 MockAgent。
- 审计/文件目前是本地 DuckDB + 本地磁盘实现。
