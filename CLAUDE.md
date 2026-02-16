# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Conductor is a VS Code collaboration extension with a FastAPI backend. It enables team chat via WebSocket, Live Share session management, file sharing, and AI-assisted summarization/code workflows.

## Common Commands

```bash
# Setup (first time)
make setup                  # Creates .venv, installs backend + extension deps

# Run backend (dev mode with auto-reload, port 8000)
make run-backend

# Compile extension (TypeScript + Tailwind CSS)
make compile

# Lint extension
cd extension && npm run lint

# Run all tests
make test

# Run backend tests only
make test-backend

# Run a single backend test module
cd backend && ../.venv/bin/pytest tests/test_chat.py -v

# Run a single backend test by name
cd backend && ../.venv/bin/pytest tests/test_chat.py -v -k "test_name"

# Run extension tests (must compile first)
cd extension && npm run compile
node --test out/tests/conductorStateMachine.test.js

# Package extension
cd extension && npx @vscode/vsce package
```

## Architecture

Two runtime components communicate over REST + WebSocket:

```
VS Code Extension (TypeScript)  <-->  FastAPI Backend (Python, port 8000)
       |                                       |
       +-> Live Share                          +-> DuckDB (audit, file metadata)
                                               +-> Local filesystem (uploads/)
```

### Backend (`backend/app/`)

FastAPI application in `main.py`. Each feature is a separate module with its own router:

- **chat/**: WebSocket real-time chat with `ConnectionManager` (room-scoped connections, in-memory message history, read receipts, message dedup). Room state is in-memory only.
- **ai_provider/**: AI summarization pipeline (`pipeline.py`) with 3 stages: classification (7 discussion types) -> targeted summary -> code relevance scoring. Provider resolution (`resolver.py`) with priority: `claude_bedrock` -> `claude_direct`. Supports Anthropic direct, AWS Bedrock, and OpenAI.
- **agent/**: `MockAgent` for deterministic change generation (not LLM-based yet).
- **auth/**: AWS SSO (IAM Identity Center) device authorization flow for user identity.
- **policy/**: Auto-apply safety policy evaluation for code changes.
- **audit/**: DuckDB-based audit logging for applied changes.
- **files/**: File upload/download with room-scoped storage.
- **summary/**: Legacy keyword extraction (deprecated, router unregistered; use `ai_provider` instead).
- **config.py**: Pydantic-validated YAML config loading. Split into `conductor.secrets.yaml` (gitignored, API keys) and `conductor.settings.yaml` (commitable settings). Search order: `./config/` -> `./` -> `../config/` -> `~/.conductor/`.

### Extension (`extension/src/`)

Entry point: `extension.ts` which registers commands and sets up the WebView message bridge.

- **services/conductorStateMachine.ts**: 6-state FSM (Idle, BackendDisconnected, ReadyToHost, Hosting, Joining, Joined). Join-only mode works via `BackendDisconnected -> Joining`.
- **services/conductorController.ts**: Orchestrates FSM transitions, backend health checks, session lifecycle.
- **services/session.ts**: `globalState` persistence for room/session IDs, backend URL resolution including ngrok detection.
- **services/permissions.ts**: Role-based access (`lead` vs `member` via `aiCollab.role` VS Code setting).
- **services/diffPreview.ts**: Sequential diff preview and code change application.
- **media/chat.html**: Single-file WebView UI that communicates with the extension host via `postMessage`.

### Shared Contract

`shared/changeset.schema.json` defines the `ChangeSet` format used between backend and extension. `FileChange.type` is either `create_file` or `replace_range`.

## Configuration

Two YAML files in `config/`:
- `conductor.secrets.yaml` - API keys for Anthropic, AWS Bedrock, OpenAI, ngrok authtoken (gitignored; see `.example` files)
- `conductor.settings.yaml` - Server, ngrok, AI model, session, logging settings (commitable)

Key VS Code extension settings: `aiCollab.role` (lead/member), `aiCollab.backendUrl`, `aiCollab.autoStartLiveShare`.

## Testing

- Backend: pytest (224 tests). Tests are in `backend/tests/`, one file per module.
- Extension: Node test runner (5 test files in `extension/src/tests/`). No `npm test` script configured; run individually with `node --test`.
- Extension tests cover service logic, not VS Code UI automation. Some tests start local HTTP servers and may fail in sandboxed environments.
