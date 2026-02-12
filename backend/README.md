# Backend API

[English](#english) | [中文](#中文)

---

<a name="english"></a>
## English

Conductor backend is built with FastAPI and provides chat, files, change generation, policy evaluation, audit logs, and summary extraction.

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
| WS | `/ws/chat/{room_id}` | Real-time chat WebSocket |

#### AI Changes

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
| DELETE | `/files/room/{room_id}` | Delete all files for a room |

#### Summary

| Method | Path | Description |
|---|---|---|
| POST | `/summary` | Keyword-based structured summary extraction |

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
        "content": "print(\"updated\")\n"
      }],
      "summary": "update"
    }
  }'
```

Summary extraction:

```bash
curl -X POST http://localhost:8000/summary \
  -H "Content-Type: application/json" \
  -d '{
    "roomId": "room-1",
    "messages": [
      {"userId": "u1", "content": "Goal: ship MVP", "ts": 1},
      {"userId": "u2", "content": "Decided: use FastAPI", "ts": 2},
      {"userId": "u1", "content": "Should we use Redis?", "ts": 3}
    ]
  }'
```

File upload:

```bash
curl -X POST "http://localhost:8000/files/upload/room-1" \
  -F "file=@./demo.png" \
  -F "user_id=u1" \
  -F "display_name=Alice" \
  -F "caption=design"
```

### Storage

- Audit DB: `audit_logs.duckdb`
- File metadata DB: `file_metadata.duckdb`
- File content: `uploads/{room_id}/...`

### Tests

Current backend collection count is `104`.

```bash
cd backend
../.venv/bin/pytest tests --collect-only -q
../.venv/bin/pytest tests -v
```

Breakdown:
- `tests/test_main.py`: 1
- `tests/test_chat.py`: 8
- `tests/test_mock_agent.py`: 26
- `tests/test_auto_apply_policy.py`: 28
- `tests/test_audit.py`: 14
- `tests/test_style_loader.py`: 14
- `tests/test_summary.py`: 13

### Known Limits

- `/generate-changes` is still MockAgent-based.
- `/summary` is keyword-based (non-LLM).
- Audit and file services are local DuckDB/disk implementations.

---

<a name="中文"></a>
## 中文

Conductor 后端基于 FastAPI，提供聊天、文件、变更生成、策略评估、审计日志与摘要提取能力。

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
| GET | `/chat/{room_id}/history` | 基于时间游标的历史分页 |
| WS | `/ws/chat/{room_id}` | 实时聊天 WebSocket |

#### AI 变更

| Method | Path | 说明 |
|---|---|---|
| POST | `/generate-changes` | 生成 ChangeSet（当前为 MockAgent） |
| POST | `/policy/evaluate-auto-apply` | 评估自动应用安全性 |

#### 审计

| Method | Path | 说明 |
|---|---|---|
| POST | `/audit/log-apply` | 记录手动/自动应用操作 |
| GET | `/audit/logs` | 查询审计日志（支持按 `room_id` 过滤） |

#### 文件

| Method | Path | 说明 |
|---|---|---|
| POST | `/files/upload/{room_id}` | 向房间上传文件 |
| GET | `/files/download/{file_id}` | 下载文件 |
| DELETE | `/files/room/{room_id}` | 删除房间全部文件 |

#### 摘要

| Method | Path | 说明 |
|---|---|---|
| POST | `/summary` | 基于关键词的结构化摘要提取 |

### WebSocket 协议（核心）

连接：
- `ws://<host>/ws/chat/{room_id}`

服务端 -> 客户端：
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

客户端 -> 服务端：
- `join`
- `message`
- `file`
- `code_snippet`
- `typing`
- `read`
- `end_session`

安全模型：
- `userId` 与角色由后端分配（首个连接用户为 `host`，后续为 `guest`）。
- 敏感行为不信任客户端伪造身份。
- 仅 host 可结束会话。

### 请求示例

健康检查：

```bash
curl http://localhost:8000/health
```

生成变更（MockAgent）：

```bash
curl -X POST http://localhost:8000/generate-changes \
  -H "Content-Type: application/json" \
  -d '{
    "file_path": "src/main.py",
    "instruction": "Generate mock changes",
    "file_content": "print(\"hello\")"
  }'
```

策略评估：

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
        "content": "print(\"updated\")\n"
      }],
      "summary": "update"
    }
  }'
```

摘要提取：

```bash
curl -X POST http://localhost:8000/summary \
  -H "Content-Type: application/json" \
  -d '{
    "roomId": "room-1",
    "messages": [
      {"userId": "u1", "content": "Goal: ship MVP", "ts": 1},
      {"userId": "u2", "content": "Decided: use FastAPI", "ts": 2},
      {"userId": "u1", "content": "Should we use Redis?", "ts": 3}
    ]
  }'
```

文件上传：

```bash
curl -X POST "http://localhost:8000/files/upload/room-1" \
  -F "file=@./demo.png" \
  -F "user_id=u1" \
  -F "display_name=Alice" \
  -F "caption=design"
```

### 存储

- 审计数据库：`audit_logs.duckdb`
- 文件元数据数据库：`file_metadata.duckdb`
- 文件内容目录：`uploads/{room_id}/...`

### 测试

当前后端测试收集总数为 `104`。

```bash
cd backend
../.venv/bin/pytest tests --collect-only -q
../.venv/bin/pytest tests -v
```

分布：
- `tests/test_main.py`: 1
- `tests/test_chat.py`: 8
- `tests/test_mock_agent.py`: 26
- `tests/test_auto_apply_policy.py`: 28
- `tests/test_audit.py`: 14
- `tests/test_style_loader.py`: 14
- `tests/test_summary.py`: 13

### 当前边界

- `/generate-changes` 仍为 MockAgent。
- `/summary` 为关键词规则提取（非 LLM）。
- 审计与文件服务当前是本地 DuckDB/磁盘实现。
