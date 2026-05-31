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
make typecheck-strict  # mypy on strict-audit modules (Phase 11.3; must pass)
make typecheck      # mypy across full backend (informational — legacy has ~40 known errors)
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
- `ai_models[].explorer: true` — mark model as sub-agent capable

**Bedrock auth — two deployment modes** (same resolution in `claude_bedrock._get_client` and
`sdk_worker.bedrock_env`; mode is inferred from which creds are present, priority
**bearer > static keys > profile > IAM role**):
- **Local** — pick whichever is simplest for a long-lived token to test model performance:
  - **Bedrock API key** (recommended for local): `CONDUCTOR_AWS_BEARER_TOKEN=<key>` →
    exported as `AWS_BEARER_TOKEN_BEDROCK`; a single long-lived bearer token, no SSO login /
    profile / refresh. ⚠️ it's a long-lived secret — keep it in a sandbox account, gitignored,
    with a narrow `bedrock:InvokeModel` IAM policy.
  - **SSO profile**: `CONDUCTOR_AWS_PROFILE=<profile>` (boto3 + the CLI's AWS SDK auto-refresh
    role creds from the cached SSO login — one `aws sso login` per ~8h, no hourly pasting).
  - **Static keys**: `CONDUCTOR_AWS_ACCESS_KEY_ID` / `CONDUCTOR_AWS_SECRET_ACCESS_KEY`.
- **Deployed** — none of the above → Bedrock is reached via the ambient **IAM role**
  (ECS task role / instance profile) through the default credential chain.

Environment variables override secrets for cloud deployment (`CONDUCTOR_*` prefix):
```bash
CONDUCTOR_AWS_BEARER_TOKEN=...        # Bedrock — local Bedrock API key (bearer); highest priority
CONDUCTOR_AWS_PROFILE=...             # Bedrock — local SSO profile (auto-refresh); omit in deployed/role mode
CONDUCTOR_AWS_ACCESS_KEY_ID=...       # Bedrock — static creds (alternative to profile)
CONDUCTOR_AWS_SECRET_ACCESS_KEY=...
CONDUCTOR_AWS_REGION=eu-west-2
CONDUCTOR_POSTGRES_PASSWORD=...       # Database
CONDUCTOR_JIRA_CLIENT_ID=...          # Integrations (Jira 3LO)
CONDUCTOR_ATLASSIAN_READONLY_TOKEN=...  # Atlassian readonly (Jira + Confluence)
```
See `docs/GUIDE.md` §21.7 for the full variable reference.

## Code Quality

Backend Python code is enforced by **ruff** (linter + isort), **black** (formatter), and **mypy** (type checker on audited modules), all configured in `pyproject.toml`.

- `make lint` — auto-fix lint issues
- `make format` — auto-format with black + ruff
- `make lint-check` — CI mode (no changes, exits non-zero on violation)
- `make typecheck-strict` — mypy on the Phase 11.3 strict-audit module list (`code_review/splitter.py`, `code_review/translate.py`, `scratchpad/`). Must pass.
- `make typecheck` — mypy on the full backend, informational. Legacy modules (ai_provider resolver, older tool helpers) have known type debt; goal is to reduce the permissive-module list over time.
- All new code must pass `make lint-check` + `make typecheck-strict` before commit
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

See [ROADMAP.md](ROADMAP.md). Near-term priorities (2026-05).

**Recently shipped (Agent-SDK Migration, Steps 01–06 — COMPLETE 2026-05-31):**
- **Dual-engine dispatch** — dispatched **leaf** sub-agents now run on the **Claude Agent SDK** via `SdkWorkerRunner` (`backend/app/agent_loop/sdk_worker.py`); **coordinators** (General / Domain / PR Brain) stay in-house on `AgentLoopService`. The discriminator in `brain._dispatch_explore` routes agents holding `dispatch_*` tools → in-house, else → SDK leaf. The SDK/CLI owns the loop + compaction; we keep the moat (vault-aware MCP tools on a shared `CachedToolExecutor`, the 4-layer full-replace system prompt, a post-call evidence gate).
- **Claude-only providers** — AI providers collapsed to Bedrock Converse + Anthropic Messages (OpenAI / Alibaba / Moonshot / Qwen removed).
- **Langfuse → task telemetry** — Langfuse removed; per-worker cost/latency now via `TaskTelemetryService` + the `task` DB table.
- **Bedrock auth — two modes** — local (secret / SSO profile auto-refresh via `CONDUCTOR_AWS_PROFILE`) vs deployed (ambient IAM role / default chain); see the Configuration section above. Detail + eval gates in `docs/archive/REFACTOR_EXECUTION_LOG.md`.

**Recently shipped (PR Brain v2 productisation):**
- **Phase 9.13 PR Brain v2** — coordinator-worker agent-as-tool architecture with `dispatch_subagent` (file-range scoped, 3 checks) + `dispatch_dimension_worker` (full-diff through one role lens); 7 agent_factory role templates; legacy v1 fleet deleted.
- **Phase 9.15 Fact Vault** — task-scoped SQLite cache shared across sub-agents, existence facts, skip-list, plan memory, + **Phase 9.9.3 structured notes** (`update_notes` tool lets sub-agents persist scratch observations that survive the 3-turn context-clearing policy).
- **Phase 9.16 Forked agent pattern** — `fork_call` primitive replaces AgentLoopService dispatch for P11 verifier calls. Cache-stable PR-context prefix + ~90% input cost reduction per verifier call.
- **Phase 9.17 Brain lifecycle hooks** — 4 extension points (`on_survey_complete`, `on_dispatch_complete`, `on_synthesize_complete`, `on_task_end`). Fire-and-forget; exceptions swallowed. First consumer: scratchpad cleanup on `on_task_end`. Platform for future telemetry / consolidation / risk-classifier plugins.
- **Phase 9.18 tree-sitter hardening** — subprocess-isolated parser with SIGKILL-on-timeout; JSX-depth heuristic routes large TSX to regex; tree-sitter 0.25 + language-pack.
- **Phase 7.8 Azure DevOps Auto Review** — size gates (50-2200 lines), `translate_pr_summary` platform-shaped comments, mandatory-dispatch detector (Tier 1 path + Tier 2 `+`-line content), PR splitter (7.8.5) with teach-not-command rationales.
- **Phase 7.8.6 Atlassian readonly enrichment** — service-account Basic-auth path for Jira + Confluence (one classic API token, no 3LO consent). ADO router pre-fetches Jira tickets (regex'd from branch/title/description) + Confluence URLs (description), flattens ADF/storage XHTML to markdown-lite, splices into both the coordinator's system context and the cache-stable P11 verifier prefix. Coordinator skill teaches: anchor invariants from acceptance criteria, calibrate severity (criterion break = `critical`), catch intent drift. New `docs/JIRA_TICKET_STANDARD.md` is the spec for human + agent ticket authors.
- **Phase 7.7.11 Jira Webhook Auto-Investigate (MVP)** — `POST /api/webhooks/jira?token=...` receiver. On `jira:issue_created`, dispatches a background asyncio task that fetches the ticket via the readonly client, runs a single zero-tool LLM triage call (Triage / Likely components / First investigation steps / Risks), and posts the result back as an ADF-formatted comment. Real code investigation deferred — needs per-project workspace mounting. Setup walkthrough in `docs/JIRA_WEBHOOK_SETUP.md`.
- **Phase 11.3 Type checking** — mypy strict-audit baseline on `code_review.splitter` / `translate` / `scratchpad/*`; CI gate for new annotated code. Full backend lint-checked with ~140 legacy debt entries tracked.
- **v2u Phase 2 reorder** — P13 deterministic (Python/Go/Java import scanners) runs BEFORE LLM existence worker; worker sees "Pre-verified by P13" block and focuses on 5 signature-level checks; timeout 120s → 60s. Sentry composite 0.796 → 0.834 (+0.038), catch 7/10 → 8/10, zero OOM after Makefile serial-suite fix.
- **Phase 9.19 Domain Brain** — specialised orchestrator for business-flow / domain logic queries. General Brain hands off via `transfer_to_brain("domain")` → `DomainBrainOrchestrator` → coordinator self-survey (mandatory project-doc read + domain-anchor grep) → parallel `dispatch_explore(template=...)` workers → coverage check → synthesis with 8 preserve-specifics rules + 4-section format. Replaces the old `dispatch_swarm("business_flow")` path. Eval (5 cases / agent_quality): 94-98% AVG, 3/5 cases hit 100%. Driving skill: `config/skills/domain_brain_coordinator.md`.
- **Phase 9.19 dispatch primitive rename + folder reorg** — `dispatch_agent` / `dispatch_subagent` / `dispatch_dimension_worker` → `dispatch_explore` / `dispatch_verify` / `dispatch_sweep` (intent-naming: open prose / scope+checks JSON / full-diff one-lens). Param schemas + tool defs moved into `backend/app/agent_loop/dispatch/{explore,verify,sweep}.py`; handlers stay in `brain.py` (12+ executor-state couplings).
- **Phase 9.19 dispatch_swarm retired** — `_dispatch_swarm` handler + `DispatchSwarmParams` deleted, `config/swarms/business_flow.yaml` + `config/agents/explore_synthesizer.md` deleted. Domain Brain replaces this path; `load_swarm_registry()` kept (returns `{}`) for back-compat.
- **Phase 9.18.1 workspace scan pruning** — `_scan_workspace` rewritten from `ws.rglob("*")` (walked all 293K files in render then filtered) to `os.walk` with in-place `dirnames[:]` pruning. Render: 293K files / 270s → 10K files / 18s (96.5% reduction, **14.5x faster**). Extended exclude list with `target/`, `.gradle/`, `out/`, `bin/`, `.idea/`, `.vscode/`, `.next/`, `.nuxt/`, `coverage/`, `.tox/`, `.ruff_cache/`, `.m2/`, `classes/`, `.venv/`. Graph cache TTL 120s → 1800s with `CONDUCTOR_GRAPH_TTL_S` env override.

**Immediate (Sprint 14–16):**
- **Phase 12: Team Knowledge Base** — Postgres + pgvector, auto-ingest from summaries, context injection into Brain/Summary/Review
- **Phase 7.5: Teams Bot Integration** — `@Conductor summarize` in Teams channels, lightweight + deep (with KB) modes
- **Phase 13: AI Summary → Action Pipeline** — `/plan` command bridges summary → Jira tickets + TODOs, one-click workflow

**Ongoing:**
- **Phase 9: Claude Code Pattern Adoption + Competitive Analysis** — agent loop recovery, streaming tools, prompt caching + monthly Cline/CodeRabbit/Cursor study (`reference/competitive/`)
- **Phase 7.7.10-7.7.12: Jira Advanced** — webhook auto-investigate, MCP server, auto branch + PR creation
- **Phase 11: Engineering Infrastructure** — CI/CD, type checking, observability expansion
