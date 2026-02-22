# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Conductor is a VS Code collaboration extension with a FastAPI backend. It enables team chat via WebSocket, Live Share session management, file sharing, AI-assisted summarization/code workflows, AI code explanation, and workspace TODO scanning.

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
cd extension && npm run test              # Runs all out/tests/*.test.js

# Run a single extension test file
node --test extension/out/tests/conductorStateMachine.test.js

# Partial setup
make setup-backend              # Backend only (venv + pip install)
make setup-extension            # Extension only (npm install)

# Individual compile steps
make compile-ts                 # TypeScript only
make compile-css                # Tailwind CSS only

# Clean all generated files (venv, out/, node_modules, __pycache__)
make clean

# Package extension as .vsix (compiles first)
make package
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

FastAPI application in `main.py`. Each feature is a separate module with its own router. Modules follow a `module/{__init__.py, router.py, service.py or domain files}` convention:

- **chat/**: WebSocket real-time chat with `ConnectionManager` (room-scoped connections, in-memory message history, read receipts, message dedup). Room state is in-memory only. Room-scoped settings via `settings_router.py` (code_style, output_mode). `MessageType` enum includes `MESSAGE`, `CODE_SNIPPET`, `FILE`, `AI_SUMMARY`, `AI_CODE_PROMPT`, `AI_EXPLANATION`.
- **ai_provider/**: AI summarization pipeline (`pipeline.py`) with 4 stages: classification (7 discussion types) → targeted summary → code relevance scoring → item extraction (`CodeRelevantItem`). Provider resolution (`resolver.py`) with `ProviderType` enum and priority-based fallback across Anthropic direct, AWS Bedrock, and OpenAI. Code prompt generation (`wrapper.py`) loads style guidelines based on detected workspace languages. Fluent prompt builder (`prompt_builder.py`) with `PromptBuilder` class for language inference from components, doc-only detection, and configurable output modes (unified_diff, direct_repo_edits, plan_then_diff).
- **context/**: AI code explanation pipeline. `router.py` exposes `POST /context/explain`. `enricher.py` (`ContextEnricher`) fills missing context and calls the LLM. `skills.py` (`CodebaseSkills`) provides utilities: `extract_imports`, `extract_context_window`, `find_containing_function`, `build_explanation_prompt`. `schemas.py` defines `ExplainRequest` and `ExplainResponse`.
- **todos/**: Task tracking with DuckDB persistence. `service.py` (`TODOService`) provides CRUD for tasks with fields: title, description, type, priority, status (open/in_progress/done), file_path, line_number, created_by, assignee, source (ai_summary/manual/stack_trace/test_failure/workspace_scan). `router.py` exposes `GET/POST /todos/{room_id}` and `PUT/DELETE /todos/{room_id}/{todo_id}`.
- **agent/**: `MockAgent` for deterministic change generation (not LLM-based yet). `style_loader.py` loads Google-derived style guides for Python, Java, JavaScript, Go, JSON from `agent/styles/*.md`.
- **auth/**: SSO login via device authorization flows — AWS IAM Identity Center and Google OAuth 2.0. Shared `_poll_for_identity()` helper handles the common poll-then-resolve-identity pattern.
- **policy/**: Auto-apply safety policy evaluation for code changes (file count, line count, forbidden paths).
- **audit/**: DuckDB-based audit logging for applied changes with SHA-256 changeset hashing.
- **files/**: File upload/download with room-scoped storage (`uploads/{room_id}/`). DuckDB metadata tracking. 20MB size limit. Duplicate file detection via `GET /files/check-duplicate/{room_id}` (case-insensitive filename match).
- **config.py**: Pydantic-validated YAML config loading. Split into `conductor.secrets.yaml` (gitignored, API keys) and `conductor.settings.yaml` (commitable settings). Search order: `./config/` → `./` → `../config/` → `~/.conductor/`.
- **ngrok_service.py**: Ngrok tunnel lifecycle (`start_ngrok`, `stop_ngrok`, `get_public_url`). Started/stopped in `main.py` lifespan.

### Extension (`extension/src/`)

Entry point: `extension.ts` which registers commands and sets up the WebView message bridge. File uploads use Node.js built-in `FormData` + `Blob` with retry logic (3 attempts) for both upload and duplicate check requests. Backend URLs are normalized (`localhost` → `127.0.0.1`) to avoid IPv6 resolution issues in Node.js. End Chat automatically closes the active Live Share session.

**Critical architecture rule**: The WebView (`chat.html`) must **never** call `fetch()` to backend URLs directly — VS Code WebView CSP blocks connections to external/ngrok URLs. All HTTP calls go through the extension host (`extension.ts`) via `postMessage`, and the host relays responses back via `webview.postMessage`.

- **services/conductorStateMachine.ts**: 6-state FSM (Idle, BackendDisconnected, ReadyToHost, Hosting, Joining, Joined). Join-only mode works via `BackendDisconnected -> Joining`. Pure logic, no VS Code dependency.
- **services/conductorController.ts**: Orchestrates FSM transitions, backend health checks, session lifecycle.
- **services/languageDetector.ts**: Detects workspace languages via `findFiles` glob patterns (Python, Java, JavaScript/TypeScript, Go). Results cached; cache cleared on workspace folder changes. Sends `detected_languages` to backend for style-aware CGP generation.
- **services/session.ts**: `globalState` persistence for room/session IDs, backend URL resolution including ngrok detection.
- **services/permissions.ts**: Role-based access (`lead` vs `member` via `aiCollab.role` VS Code setting).
- **services/diffPreview.ts**: Sequential diff preview and code change application.
- **services/backendHealthCheck.ts**: Stateless async health check against `GET /health`, no VS Code API dependency.
- **services/ssoIdentityCache.ts**: SSO identity storage with 24h expiry, provider tagging (`aws`/`google`), globalState persistence.
- **services/contextGatherer.ts**: Enriches a code snippet with workspace context before sending to `POST /context/explain`. Gathers: full file content (capped at 40 KB), surrounding ±15 lines, import statements (language-specific regex), enclosing function/class signature (pattern matching), and related files via LSP definitions/references. Returns a `ContextBundle`.
- **services/stackTraceParser.ts**: Parses raw stack trace text into structured `ParsedStackTrace` objects. Supports Python, JavaScript/TypeScript, Java, Go. Resolves frame paths to workspace-relative paths. Used when users paste or share stack traces in chat.
- **services/todoScanner.ts**: Scans workspace files for structured TODO comments. Format: `// TODO: title` optionally followed by `// TODO_DESC: description`. Returns `WorkspaceTodo[]` with file path, line number, title, description, and comment prefix. `updateWorkspaceTodoInFile()` writes edits back to source. Excludes `node_modules`, `.venv`, `out/`, `dist/`, `__pycache__`, `.git`.
- **media/chat.html**: Single-file WebView UI (all JS inline). Communicates with extension host via `postMessage`. Header uses a 2-row compact layout (brand row + session action bar). Tabs use a pill/segment control style. Code snippet messages use `whitespace-pre-wrap` to prevent horizontal overflow.

### Shared Contract

`shared/changeset.schema.json` defines the `ChangeSet` format used between backend and extension. `FileChange.type` is either `create_file` or `replace_range`.

## Configuration

Two YAML files in `config/`:
- `conductor.secrets.yaml` (gitignored; see `.example` files) — sections: `ai_providers` (anthropic, aws_bedrock, openai), `google_sso` (client_id, client_secret), `ngrok` (authtoken)
- `conductor.settings.yaml` (commitable) — sections: `server`, `ngrok`, `sso`, `google_sso`, `summary`, `ai_provider_settings`, `ai_models`, `session`, `change_limits`, `logging`, `prompt`

Key VS Code extension settings: `aiCollab.role` (lead/member), `aiCollab.backendUrl`, `aiCollab.autoStartLiveShare`.

## Key Data Flows

### CGP (Code Generation Prompt) Flow
1. Extension detects workspace languages (`languageDetector.ts`)
2. Extension sends `POST /ai/code-prompt` with `decision_summary`, `room_id`, `detected_languages`
3. Backend loads style guidelines: room-level override > detected languages (universal + language-specific `.md` files) > fallback universal only
4. `PromptBuilder` constructs CGP with language inference from affected components, doc-only detection, and configurable output mode (unified_diff, direct_repo_edits, plan_then_diff)
5. Response sent back to WebView for display

### AI Summarization Pipeline Flow
1. Extension sends chat messages via `POST /ai/summarize`
2. Stage 1: Classify discussion type (7 types: api_design, product_flow, code_change, architecture, innovation, debugging, general)
3. Stage 2: Generate targeted summary with type-specific prompt
4. Stage 3: Compute code-relevant types for selective CGP generation
5. Stage 4: Extract actionable items as `CodeRelevantItem` list
6. Response sent back with `PipelineSummary` including classification metadata and extracted items

### Explain Code Flow
1. User selects code in VS Code editor, clicks the 💡 button in the chat toolbar
2. WebView sends `getCodeSnippet` to extension host
3. Extension host captures the current editor selection (file, lines, language)
4. `ContextGatherer.gather()` enriches the snippet: reads full file, extracts surrounding lines, imports, enclosing function, and related files via LSP
5. Extension host calls `POST /context/explain` with the enriched context bundle
6. `ContextEnricher` on the backend fills any remaining gaps and calls the LLM
7. Extension host receives the explanation and posts it to the chat room via `POST /chat/{room_id}/ai-message` (type `ai_explanation`) — **not** from the WebView (CSP restriction)
8. Backend broadcasts `ai_explanation` message via WebSocket to all room participants
9. WebView renders `renderAiExplanationMessage()` with the explanation and a navigate-to-code button

### Workspace TODO Scanner Flow
1. User clicks **Scan** in the Tasks → Code TODOs section
2. WebView sends `scanWorkspaceTodos` to extension host
3. `todoScanner.scanWorkspaceTodos()` traverses all workspace folders, reads source files, and matches lines against `// TODO: title` (and `// TODO_DESC: description`) patterns
4. Returns `WorkspaceTodo[]` sorted by file path then line number
5. WebView renders the list; clicking an item navigates to its source location
6. User can edit title/description in a modal; Save sends `updateWorkspaceTodo` to extension host
7. `updateWorkspaceTodoInFile()` rewrites the relevant comment lines in the source file

## Testing

- Backend: pytest (368 tests). Tests are in `backend/tests/`, one file per module. Shared fixtures in `tests/conftest.py`.
- Extension: Node test runner (5 test files in `extension/src/tests/`). Run all with `cd extension && npm run test` or individually with `node --test`.
- Extension tests cover service logic, not VS Code UI automation. Some tests start local HTTP servers and may fail in sandboxed environments.

## Related Documentation

- `ROADMAP.md` — Future project plan (5 phases: production readiness, LLM agent, collaboration features, security, scalability)
- `docs/GUIDE.md` — Code walkthrough for engineers (architecture, patterns, data flows)
- `docs/ARCHITECTURE.md` — Concise architecture reference with runtime sequences
