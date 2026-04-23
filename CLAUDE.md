# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Conductor is a VS Code collaboration extension with a FastAPI backend. Two main parts:
1. **`extension/`** — TypeScript VS Code extension
2. **`backend/`** — Python FastAPI server

Detailed architecture docs live in subdirectory CLAUDE.md files:
- `backend/CLAUDE.md` — Backend structure, Brain orchestrator, Code review, Key patterns
- `extension/CLAUDE.md` — Extension structure, Local mode tool dispatch, Chat WebView
- `config/CLAUDE.md` — Agent & prompt design principles, 4-layer architecture
- `eval/CLAUDE.md` — Eval system commands and scoring
- `reference/CLAUDE.md` — Claude Code source study notes

## Commands

### Quick Start
```bash
make setup          # create venv + install all dependencies
make data-up        # start Postgres + Redis (Docker)
make db-update      # apply Liquibase schema migrations
make run-backend    # start backend (dev mode, auto-reload)
make test           # run all tests (backend + extension + webview + parity)
make test-frontend  # run all frontend tests (extension services + React WebView)
make test-webview   # run React WebView tests only (vitest)
make package        # compile and package extension as .vsix
make test-parity    # validate Python↔TS tool parity
make lint           # lint backend Python (ruff, auto-fix)
make format         # format backend Python (black + ruff format)
make lint-check     # lint + format check (CI mode, no changes)
make langfuse-up    # start self-hosted Langfuse (port 3001)
make update-prompt-library   # download latest prompts.chat CSV (agent design reference)
```

### Backend (Python/FastAPI)
```bash
cd backend
uvicorn app.main:app --reload
pytest                             # all tests
pytest -k "test_agent_loop"       # filter by name
pytest tests/test_code_tools.py -v
pytest --cov=. --cov-report=html
```

### Extension (TypeScript/VS Code)
```bash
cd extension
npm run compile           # one-time build (TS + React WebView + CSS)
npm run compile:webview   # rebuild React WebView only
npm run watch             # watch mode (TS only)
npm run watch:webview     # watch React WebView
npm test                  # 321 extension service tests (node:test)
npm run test:webview      # 151 React WebView tests (vitest)
# F5 in VS Code → "Run VS Code Extension" to debug
```

## Tool Change Process

When modifying or adding a code tool:

1. **Python first**: implement/modify in `backend/app/code_tools/tools.py`
2. **Update schema**: if params/result shape changed, update `schemas.py`
3. **Update metadata**: add/update entry in `TOOL_METADATA` dict in `schemas.py` (is_read_only, is_concurrent_safe, summary_template, category)
4. **Regenerate contracts**: `make update-contracts`
5. **Port to TS**: update the appropriate module:
   - Complex: `extension/src/services/complexToolRunner.ts`
   - AST: `extension/src/services/astToolRunner.ts`
6. **Update dispatcher**: add to appropriate set in `localToolDispatcher.ts` (SUBPROCESS/AST/COMPLEX)
7. **Add parity tests**: `test_tool_parity_ast.py` or `test_tool_parity_deep.py`
8. **Validate**: `make test-parity`

## Configuration

```bash
cp config/conductor.secrets.yaml.example config/conductor.secrets.yaml
# Fill in API keys
```

Key settings in `conductor.settings.yaml`:
- `langfuse.enabled` + secrets in `conductor.secrets.yaml`
- `ai_models[].explorer: true` — mark model as sub-agent capable

Environment variables override secrets for cloud deployment (`CONDUCTOR_*` prefix):
```bash
CONDUCTOR_AWS_ACCESS_KEY_ID=...       # Bedrock credentials
CONDUCTOR_AWS_SECRET_ACCESS_KEY=...
CONDUCTOR_AWS_REGION=eu-west-2
CONDUCTOR_POSTGRES_PASSWORD=...       # Database
CONDUCTOR_JIRA_CLIENT_ID=...          # Integrations
LANGFUSE_PUBLIC_KEY=...               # Observability
LANGFUSE_SECRET_KEY=...
```
See `docs/GUIDE.md` §21.7 for the full variable reference.

## Code Quality

Backend Python code is enforced by **ruff** (linter + isort) and **black** (formatter), configured in `pyproject.toml`.

- `make lint` — auto-fix lint issues
- `make format` — auto-format with black + ruff
- `make lint-check` — CI mode (no changes, exits non-zero on violation)
- All new code must pass `make lint-check` before commit
- Pre-commit hooks available: `pip install pre-commit && pre-commit install`

Extension TypeScript uses ESLint (`.eslintrc.json`) with safety rules (`semi`, `curly`, `eqeqeq`, `no-throw-literal`) set to `error`.

## Testing Notes

- Backend: `pytest` with mocked external dependencies. See `backend/CLAUDE.md` for full test file list.
- Extension services: `npm test` (321 tests, node:test). See `extension/CLAUDE.md` for tool parity testing.
- React WebView: `npm run test:webview` (151 tests, vitest + jsdom). Covers reducers, slash commands, message parsing, component behavior.
- `conftest.py`: stubs for cocoindex, sentence_transformers, sqlite_vec
- Agent loop tests: `MockProvider` subclass with scripted responses
- Full frontend: `make test-frontend` (472 tests = 321 service + 151 WebView)

## What's Next

See [ROADMAP.md](ROADMAP.md). Near-term priorities (2026-04):

**Recently shipped (PR Brain v2 productisation):**
- **Phase 9.13 PR Brain v2** — coordinator-worker agent-as-tool architecture with `dispatch_subagent` (file-range scoped, 3 checks) + `dispatch_dimension_worker` (full-diff through one role lens); 7 agent_factory role templates; legacy v1 fleet deleted.
- **Phase 9.15 Fact Vault** — task-scoped SQLite cache shared across sub-agents, existence facts, skip-list, plan memory.
- **Phase 9.18 tree-sitter hardening** — subprocess-isolated parser with SIGKILL-on-timeout; JSX-depth heuristic routes large TSX to regex; tree-sitter 0.25 + language-pack.
- **Phase 7.8 Azure DevOps Auto Review** — size gates (50-2200 lines), `translate_pr_summary` platform-shaped comments, mandatory-dispatch detector (Tier 1 path + Tier 2 `+`-line content), PR splitter (7.8.5) with teach-not-command rationales.
- **v2u Phase 2 reorder** — P13 deterministic (Python/Go/Java import scanners) runs BEFORE LLM existence worker; worker sees "Pre-verified by P13" block and focuses on 5 signature-level checks; timeout 120s → 60s. Sentry composite 0.796 → 0.834 (+0.038), catch 7/10 → 8/10, zero OOM after Makefile serial-suite fix.

**Immediate (Sprint 14–16):**
- **Phase 12: Team Knowledge Base** — Postgres + pgvector, auto-ingest from summaries, context injection into Brain/Summary/Review
- **Phase 7.5: Teams Bot Integration** — `@Conductor summarize` in Teams channels, lightweight + deep (with KB) modes
- **Phase 13: AI Summary → Action Pipeline** — `/plan` command bridges summary → Jira tickets + TODOs, one-click workflow

**Ongoing:**
- **Phase 9: Claude Code Pattern Adoption + Competitive Analysis** — agent loop recovery, streaming tools, prompt caching + monthly Cline/CodeRabbit/Cursor study (`reference/competitive/`)
- **Phase 7.7.10-7.7.12: Jira Advanced** — webhook auto-investigate, MCP server, auto branch + PR creation
- **Phase 11: Engineering Infrastructure** — CI/CD, type checking, observability expansion
