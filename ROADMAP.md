# Conductor Project Roadmap

Last updated: 2026-02-17

## Current State

Conductor is a VS Code collaboration extension with a FastAPI backend. It supports real-time team chat over WebSocket, Live Share session management, file sharing, SSO authentication (AWS IAM Identity Center + Google OAuth), AI-powered discussion summarization, and code generation prompt (CGP) workflows.

**Backend stack**: FastAPI, DuckDB (audit + file metadata), in-memory chat, pyngrok
**Extension stack**: TypeScript, VS Code WebView, Live Share API
**AI providers**: Anthropic direct, AWS Bedrock, OpenAI (priority-based fallback)

### What Works Today

| Feature | Status | Notes |
|---------|--------|-------|
| Real-time chat (WebSocket) | Working | In-memory only, lost on restart |
| AI summarization pipeline | Working | 4-stage: classify -> summarize -> score -> extract items |
| Code generation prompts | Working | With auto-detected language styles |
| File upload/download | Working | Room-scoped, DuckDB metadata, duplicate detection, retry logic |
| SSO (AWS + Google) | Working | Device authorization flow |
| Auto-apply policy | Working | Basic: file count, line count, forbidden paths |
| Audit logging | Working | DuckDB, tracks applied changes |
| Ngrok tunneling | Working | Auto-start/stop in lifespan |
| Mock agent | Working | Deterministic only, not LLM-backed |

---

## Phase 1: Production Readiness

Goal: Make the system reliable enough for daily team use. Focus on data durability, resource management, and enforcing existing configuration.

### 1.1 Chat Message Persistence

**Problem**: All chat messages are stored in-memory (`chat/manager.py:219`). Server restart = total data loss. No audit trail for compliance.

**Plan**:
- Add a `chat_messages` table in DuckDB alongside the existing audit and file metadata tables
- Schema: `(id, room_id, user_id, display_name, role, content, message_type, ts)`
- Index on `(room_id, ts)` for paginated retrieval
- Write-through: persist on receive, serve from memory for active rooms
- Add `GET /chat/{room_id}/history?before=<ts>&limit=50` endpoint for pagination
- Load recent history into memory when a room becomes active

**Files to modify**: `chat/manager.py`, `chat/router.py`, new `chat/service.py`

### 1.2 Room Lifecycle Management

**Problem**: Rooms persist in memory indefinitely. No TTL, no cleanup for abandoned sessions. The `timeout_minutes` and `max_participants` config values exist but are never enforced.

**Plan**:
- Track `created_at` and `last_activity_at` per room in `ConnectionManager`
- Add a background task (FastAPI `BackgroundTasks` or asyncio periodic) that runs every 5 minutes:
  - Rooms with no connections and `last_activity > timeout_minutes` get archived and cleared
- Enforce `max_participants` in the WebSocket connect handler — reject with 403 if room is full
- On shutdown (lifespan), persist active room state to DuckDB for recovery

**Files to modify**: `chat/manager.py`, `chat/router.py`, `main.py` (lifespan)

### 1.3 Configuration Enforcement

**Problem**: Several config values are defined in `conductor.settings.yaml` and parsed by `config.py` but never read at runtime.

| Config Key | Defined In | Enforced? |
|-----------|------------|-----------|
| `change_limits.max_files` | settings.yaml | No — hardcoded to 2 in `auto_apply.py:41` |
| `change_limits.max_lines_changed` | settings.yaml | No — hardcoded to 50 in `auto_apply.py:42` |
| `session.timeout_minutes` | settings.yaml | No — no timeout logic exists |
| `session.max_participants` | settings.yaml | No — no validation on join |
| `logging.level` | settings.yaml | No — hardcoded `INFO`/`DEBUG` in multiple files |

**Plan**:
- `auto_apply.py`: Replace hardcoded `MAX_FILES`, `MAX_LINES_CHANGED` with `get_config().change_limits.*`
- `chat/router.py`: Add participant count check in WebSocket connect
- `main.py`: Read `config.logging.level` and call `logging.basicConfig(level=...)` accordingly
- Remove hardcoded `logging.basicConfig(level=logging.DEBUG)` from `chat/router.py:35`

**Files to modify**: `policy/auto_apply.py`, `chat/router.py`, `main.py`

### 1.4 Structured Error Responses

**Problem**: Error responses are inconsistent across endpoints — some return `{"detail": "..."}`, others return `{"error": "..."}`, and some return raw text.

**Plan**:
- Define a standard `ErrorResponse` model: `{"error": str, "detail": Optional[str]}`
- Add a global exception handler in `main.py` for consistent formatting
- Audit all endpoints for error response consistency

---

## Phase 2: LLM Agent Integration

Goal: Replace the mock agent with real LLM-backed code generation. This is the core value proposition of the product.

### 2.1 LLM-Backed Agent

**Problem**: `MockAgent` generates deterministic, hardcoded changes. It does not analyze existing code, respect project structure, or use AI.

**Plan**:
- Create `LLMAgent` class implementing the same `generate_changes()` interface
- Input: file path, instruction, file content, project context (detected languages, style guidelines)
- Use the existing `AIProviderResolver` to select provider
- Output: `ChangeSet` conforming to `shared/changeset.schema.json`
- Keep `MockAgent` as a fallback for testing and offline use
- Selection via config: `agent.type: "llm" | "mock"`

**Files**: New `agent/llm_agent.py`, modify `agent/router.py`, `config.py`

### 2.2 Project Context Gathering

**Problem**: The agent receives only a single file path and instruction. No awareness of project structure, dependencies, or conventions.

**Partial progress**: `PromptBuilder` (`ai_provider/prompt_builder.py`) already supports language inference from affected components and configurable output modes. Room settings (`settings_router.py`) allow per-room code_style and output_mode configuration.

**Plan**:
- Extension sends workspace metadata alongside change requests:
  - Detected languages (already implemented via `languageDetector.ts`)
  - Open file contents (active editor context)
  - Project structure summary (directory tree, key files)
- Backend aggregates context into the LLM prompt
- Respect token limits — truncate/summarize context that exceeds provider limits

### 2.3 Multi-File Change Generation

**Problem**: Current `ChangeSet` schema supports multi-file changes but `MockAgent` always produces a fixed 3-file output.

**Plan**:
- LLM agent analyzes which files need modification based on the instruction
- Generate `replace_range` changes for existing files (not just `create_file`)
- Add a validation step: parse generated changes, verify file paths exist, check syntax
- Present a diff preview to the user before applying

---

## Phase 3: Collaboration Features

Goal: Make the chat experience richer and more useful for engineering teams.

### 3.1 Message Search

**Plan**:
- Add DuckDB full-text index on `chat_messages.content`
- New endpoint: `GET /chat/{room_id}/search?q=<query>&limit=20`
- Extension UI: search bar in chat panel with result highlighting

### 3.2 Message Threading

**Plan**:
- Add `parent_message_id` field to message schema
- Threads displayed as collapsible replies in the WebView
- Thread-level notifications

### 3.3 User Presence

**Plan**:
- Track `last_seen_at` per user per room (update on any WebSocket message)
- Broadcast presence status: online (active connection), idle (no message in 5min), offline
- Display status indicators in the participant list

### 3.4 Typing Indicators

**Plan**:
- New WebSocket message type: `typing_start` / `typing_stop`
- Debounced on the extension side (send `typing_start` on keypress, `typing_stop` after 3s idle)
- Display "X is typing..." in chat UI

---

## Phase 4: Security and Observability

Goal: Harden the system for enterprise deployment.

### 4.1 Rate Limiting

**Plan**:
- Add per-IP rate limiting to public endpoints (`/auth/*`, `/health`, WebSocket connect)
- Use `slowapi` or custom middleware
- Config: `security.rate_limit_per_minute: 60`

### 4.2 Input Validation Hardening

**Plan**:
- Enforce max message length in WebSocket handler (currently unbounded)
- Validate `room_id` format (alphanumeric + hyphens, max 64 chars)
- Validate `user_id` format
- Sanitize file upload filenames (path traversal prevention)

### 4.3 Expanded Audit Trail

**Problem**: Audit logs only record "change applied" events. No record of session events, policy violations, or SSO logins.

**Plan**:
- New event types: `session_start`, `session_end`, `user_join`, `user_leave`, `policy_violation`, `sso_login`
- Unified `audit_events` table with `event_type` discriminator
- Retention policy: configurable via `audit.retention_days`

### 4.4 Structured Logging and Metrics

**Plan**:
- Replace `logging.basicConfig` with structured JSON logging (e.g., `python-json-logger`)
- Add key metrics:
  - Active rooms count
  - Messages per minute
  - AI provider latency (p50, p95, p99)
  - WebSocket connection count
- Optional: OpenTelemetry integration for distributed tracing

---

## Phase 5: Scalability

Goal: Support multiple backend instances and larger teams.

### 5.1 External Message Broker

**Problem**: `ConnectionManager` uses in-memory state. Cannot scale horizontally — each backend instance has its own rooms and connections.

**Plan**:
- Replace in-memory message passing with Redis Pub/Sub (or NATS)
- Each backend instance subscribes to room channels
- Connection state remains local; message routing becomes distributed
- Preserve the current `ConnectionManager` interface for backward compatibility

### 5.2 Database Migration to PostgreSQL

**Problem**: DuckDB is single-writer. Cannot support multiple backend instances writing concurrently.

**Plan**:
- Migrate audit, file metadata, and chat messages to PostgreSQL
- Use SQLAlchemy or asyncpg for async database access
- Add Alembic for schema migrations
- Keep DuckDB as an option for single-instance deployments via config

### 5.3 File Storage Backend

**Problem**: Files stored on local filesystem (`uploads/` directory). Not shared across instances. The TODO in `files/service.py:253-259` mentions S3/GCS/Azure backup.

**Plan**:
- Add pluggable storage backend: `local` (current), `s3`, `gcs`
- Config: `files.storage_backend: "local" | "s3"`
- Implement `S3FileStorage` with same interface as current local storage
- Migrate existing files on deployment

---

## Implementation Priority

```
Now ──────────────────────────────────────────────────── Future

Phase 1                Phase 2           Phase 3        Phase 4-5
Production Ready       LLM Agent         Collab UX      Enterprise

1.1 Message persist    2.1 LLM agent     3.1 Search     4.1 Rate limits
1.2 Room lifecycle     2.2 Context        3.2 Threads    4.2 Input validation
1.3 Config enforce     2.3 Multi-file     3.3 Presence   4.3 Audit expansion
1.4 Error responses                       3.4 Typing     5.1 Redis pub/sub
                                                         5.2 PostgreSQL
                                                         5.3 S3 storage
```

**Recommended order**: 1.3 (quick win, config enforcement) -> 1.1 (persistence is critical) -> 1.2 (prevents memory leaks) -> 2.1 (core product value) -> rest follows naturally.
