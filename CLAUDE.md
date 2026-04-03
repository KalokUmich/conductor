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
make test           # run all tests (backend + extension)
make package        # compile and package extension as .vsix
make test-parity    # validate Python↔TS tool parity
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
npm run compile    # one-time build
npm run watch      # watch mode
npm test
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

## Testing Notes

- Backend: `pytest` with mocked external dependencies. See `backend/CLAUDE.md` for full test file list.
- Extension: `npm test`. See `extension/CLAUDE.md` for tool parity testing.
- `conftest.py`: stubs for cocoindex, sentence_transformers, sqlite_vec
- Agent loop tests: `MockProvider` subclass with scripted responses

## What's Next

See [ROADMAP.md](ROADMAP.md). Near-term priorities:
- **Phase 7.7.10-7.7.12: Jira Advanced** — webhook auto-investigate, MCP server, auto branch + PR creation
- **Phase 9: Claude Code Pattern Adoption** — systematic learning and integration from reference codebase
- **Phase 10: Companion & Developer Experience** — interactive mascot in VS Code WebView (CSS/SVG animations, deterministic gacha, agent integration)
- Microsoft Teams integration (Phase 7.5)
- Slack integration (Phase 7.6)
- Model B delegate authentication (no PAT required — Phase 5.1)
- Cross-session query patterns (learn from session traces — Phase 5.5)
- Persistent codebase memory (background file-summary indexer — Phase 5.5.2)
