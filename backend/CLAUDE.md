# Backend CLAUDE.md

## Structure

```
backend/app/
├── main.py                  # FastAPI app, lifespan, router registration + service startup init
├── config.py                # Settings + Secrets from YAML
├── agent_loop/              # Agentic code intelligence (LLM + tools)
│   ├── service.py           # AgentLoopService — LLM loop, tool dispatch
│   ├── brain.py             # AgentToolExecutor — dispatch_agent/dispatch_swarm/transfer_to_brain
│   ├── pr_brain.py          # PRBrainOrchestrator — deterministic PR review pipeline via Brain
│   ├── budget.py            # BudgetController — token-based budget management
│   ├── trace.py             # SessionTrace — per-session trace (Postgres + local JSON fallback)
│   ├── evidence.py          # EvidenceEvaluator — rule-based answer quality check
│   ├── completeness.py      # CompletenessCheck — verifies answer covers all query aspects
│   ├── interactive.py       # ask_user coordination (register/submit/cleanup)
│   ├── prompts.py           # 4-layer prompt architecture (Identity + Tools + Skills + Task)
│   └── router.py            # POST /api/context/query
├── chat/                    # WebSocket chat + persistence
│   ├── manager.py           # ConnectionManager — WebSocket room management
│   ├── redis_store.py       # Redis-backed hot message cache (6h TTL)
│   ├── persistence.py       # ChatPersistenceService — write-through micro-batch Postgres
│   └── router.py            # /ws/chat/{room_id}, /chat/{room_id}/history, DELETE /chat/{room_id}
├── browser/                 # Playwright-based web browsing tools
│   ├── service.py           # BrowserService — Chromium automation (Playwright)
│   ├── tools.py             # browse_url, search_web, screenshot tool implementations
│   └── router.py            # /api/browser/ endpoints
├── workflow/                # Brain orchestrator host + agent/swarm config loading
│   ├── models.py            # Pydantic models: AgentConfig, BrainConfig, SwarmConfig
│   ├── loader.py            # load_agent() / load_brain_config() / load_swarm_registry() — YAML + Markdown
│   ├── engine.py            # WorkflowEngine.run_brain_stream() — Brain orchestrator entry point
│   ├── router.py            # /api/brain/swarms — Agent Swarm UI tab data source
│   └── observability.py     # Langfuse @observe decorator (no-op when disabled)
├── code_review/             # Multi-agent PR review pipeline
│   ├── service.py           # CodeReviewService — legacy 10-step review pipeline
│   ├── shared.py            # Shared functions (used by both CodeReviewService + PRBrain)
│   ├── agents.py            # Specialized review agents (parallel dispatch)
│   ├── models.py            # PRContext, ReviewFinding, ReviewResult, RiskProfile
│   ├── diff_parser.py       # Parse git diff into PRContext
│   ├── risk_classifier.py   # Risk classification (5 dimensions)
│   ├── ranking.py           # Score and rank findings
│   ├── dedup.py             # Merge and deduplicate findings
│   └── router.py            # /api/code-review/ endpoints (+ SSE stream)
├── code_tools/              # 43 tools (code + file-edit + Jira + browser + Fact Vault) + ToolMetadata
│   ├── schemas.py           # Pydantic models + TOOL_DEFINITIONS + ToolMetadata (43 entries)
│   ├── tools.py             # Tool implementations (including glob, enhanced grep, search_facts)
│   ├── output_policy.py     # Per-tool truncation policies (budget-adaptive)
│   ├── __main__.py          # Python CLI: python -m app.code_tools <tool> <ws> '<params>'
│   └── router.py            # /api/code-tools/ endpoints
├── scratchpad/              # Phase 9.15 Fact Vault — per-session SQLite fact cache + CachedToolExecutor wrapper + search_facts + in-flight dedup
├── langextract/             # LangExtract + multi-vendor Bedrock integration
├── ai_provider/             # LLM provider abstraction (Bedrock, Direct, OpenAI)
│   ├── base.py              # AIProvider ABC + ToolCall/ToolUseResponse/TokenUsage
│   ├── claude_bedrock.py    # Bedrock Converse API
│   ├── claude_direct.py     # Anthropic Messages API
│   ├── openai_provider.py   # OpenAI Chat Completions
│   └── resolver.py          # ProviderResolver — health checks, selection
├── repo_graph/              # AST-based symbol extraction + dependency graph
├── git_workspace/           # Git workspace management (Model A)
└── integrations/            # External service integrations
    └── jira/                # Jira OAuth 3LO + REST API
        ├── service.py       # JiraOAuthService — OAuth token lifecycle + API calls
        ├── models.py        # JiraTokenPair, JiraProject, JiraIssue, CreateIssueRequest
        └── router.py        # /api/integrations/jira/* endpoints
```

## Brain Orchestrator (Agentic Code Intelligence)

The **Brain** is an LLM orchestrator (strong model) that replaces the keyword classifier.
It understands queries, dispatches specialist agents, evaluates findings, and synthesizes answers.

```
Query → Brain (Sonnet, meta-tools: dispatch_agent, dispatch_swarm, transfer_to_brain, ask_user)
  → Brain decides: SIMPLE (1 agent) | COMPLEX (handoff) | SWARM (parallel) | TRANSFER (specialized brain)
  → dispatch_agent → AgentLoopService (Haiku, code tools, isolated context)
    → 4-layer prompt: L1 system (agent identity) + L2 tools + L3 skills (workspace) + L4 query
    → Tool execution (up to 20 iter / 420K tokens)
    → Evidence check (internal retry)
    → Returns condensed AgentFindings to Brain
  → Brain synthesizes final answer with file:line evidence
```

**Sub-agent prompt assembly (4-layer):**
- **Layer 1 (system prompt)**: Built per-agent from `.md` description + instructions — defines who this agent is. Includes anti-overexploration guidance ("commit to a direction, stop when you have enough evidence").
- **Layer 2 (tools)**: `brain.yaml` core_tools ∪ agent `.md` tools ∪ signal_blocker. Tool descriptions enriched to 3-4 sentences each (when to use, when NOT to use, what it does NOT return).
- **Layer 3 (skills)**: Workspace layout, project docs, investigation patterns, risk signals, budget — shared across agents. PR review agents get `code_review_pr` skill. Business flow agents get 4-step investigation skill (identify targets → domain models → service code → separate mandatory vs conditional). Includes convergence guidance ("stop at iteration 6-7 if you have strong evidence").
- **Layer 4 (user message)**: The query from Brain + optional code_context — no role injection

**Context management:** Sub-agents clear old tool results after 3 turns to prevent context rot. Only the most recent 4 turn-pairs keep full tool output; older results are replaced with metadata-driven summaries (e.g., `grep 'auth' in src/: 12 matches`) via `ToolMetadata.summary_template` — falls back to first-line truncation if no template is available.

**Four dispatch modes:**
- **SIMPLE** (~80%): one agent, trust result, done
- **COMPLEX** (~15%): agent → evaluate → handoff to different specialist with previous findings
- **SWARM** (~5%): `dispatch_swarm("business_flow")` — predefined parallel presets. Brain must decompose queries into 3-6 search targets before dispatching.
- **TRANSFER**: `transfer_to_brain("pr_review")` — one-way handoff to specialized Brain (PR reviews)

**PR Brain** (`agent_loop/pr_brain.py`): Specialized deterministic pipeline for PR reviews. Activated via `transfer_to_brain("pr_review")`. Combines Brain's 4-layer prompts with CodeReviewService's deterministic post-processing:

```
transfer_to_brain("pr_review") → PRBrainOrchestrator
  Phase 1: Pre-compute (parse_diff, classify_risk, prefetch_diffs, impact_graph)
  Phase 2: Dispatch review agents (correctness[strong], security, reliability, concurrency, test_coverage)
  Phase 3: Post-process (evidence_gate → post_filter → dedup → score_and_rank)
  Phase 4: Adversarial arbitration (pr_arbitrator tries to rebut each finding)
  Phase 5: Merge recommendation (deterministic)
  Phase 6: Synthesis — Brain as final judge (sees sub-agent evidence + arbitrator counter-evidence)
```

Key design: The arbitrator is a **defense attorney** — it tries to rebut findings with counter-evidence and a rebuttal confidence score. It does NOT adjust severity. The synthesis LLM sees both sides (prosecution + defense) and makes the final call.

**Configuration:**
- `config/brain.yaml` — Brain limits (iterations, budget, concurrency, timeout) + core_tools
- `config/brains/pr_review.yaml` — PR Brain config (agents, budget_weights, post_processing)
- `config/agents/*.md` — Agent definitions (name, description, model, tools, limits + identity instructions)
- `config/swarms/*.yaml` — Swarm presets (agent group + parallel/sequential mode + synthesis_guide)

**Interactive AI:** Brain can `ask_user` for clarification when queries have multiple valid directions. Q&A answers are cached in session and injected into Brain's prompt for reuse across sub-agents.

**43 tools** across 3 registries + 1 session-scoped:
- **Code tools** (32, `code_tools/tools.py`): `grep` (with output_mode, context_lines, case_insensitive, multiline, file_type), `read_file`, `list_files`, `glob`, `find_symbol`, `find_references`, `file_outline`, `get_dependencies`, `get_dependents`, `git_log`, `git_diff`, `git_diff_files`, `ast_search`, `get_callees`, `get_callers`, `git_blame`, `git_show`, `git_hotspots`, `find_tests`, `test_outline`, `trace_variable`, `compressed_view`, `module_summary`, `expand_symbol`, `detect_patterns`, `run_test`, `list_endpoints`, `extract_docstrings`, `db_schema`, `file_edit`, `file_write`, **`search_facts`** (Phase 9.15).
- **Jira tools** (5, `integrations/jira/tools.py`): `jira_search` (with convenience JQL: "my tickets", "my sprint", "blockers"), `jira_get_issue`, `jira_create_issue`, `jira_update_issue`, `jira_list_projects`.
- **Browser tools** (6, `browser/tools.py`): `web_search`, `web_navigate`, `web_click`, `web_fill`, `web_screenshot`, `web_extract`.

**Tool metadata** (`code_tools/schemas.py`): `ToolMetadata` dataclass with `is_read_only`, `is_concurrent_safe`, `summary_template`, `category` for all 43 tools. Used by `_clear_old_tool_results()` for readable context compaction summaries.

**PR-review scratchpad** (Phase 9.15, `app/scratchpad/`): On each PR review start, `PRBrainOrchestrator` opens a per-session SQLite at `~/.conductor/scratchpad/{task_id}-{uuid}.sqlite` and wraps the tool executor with `CachedToolExecutor`. Sub-agent tool calls are transparently deduplicated via exact-key lookup or range-intersection (read_file 100-150 satisfies later 101-130). `search_facts` lets sub-agents query the vault metadata directly. Session file + WAL sidecars are deleted on `cleanup()`. Human-readable `task_id` (e.g. `ado-MyProject-pr-12345`) is folded into the filename so concurrent PR reviews are traceable in activity logs.

**Tree-sitter scan hardening** (Phase 9.18, `app/repo_graph/parse_pool.py`): File parses run in an isolated subprocess (`forkserver` start method on POSIX) so the main process can `SIGKILL` the worker if it exceeds `CONDUCTOR_PARSE_TIMEOUT_S` (default 60s). Required because tree-sitter's Python binding holds the GIL through the C parse — no in-process timeout mechanism can interrupt it. Paired `_estimate_jsx_depth` heuristic routes large `.tsx` files with deep nesting (>15 levels, >20KB) straight to the regex extractor, bypassing the first-encounter SIGKILL budget. Parser uses `tree-sitter 0.25` + `tree-sitter-language-pack 1.6` (replaced the abandoned `tree-sitter-languages`).

Tools also accessible via `python -m app.code_tools <tool> <workspace> '<json_params>'` (used by extension local mode).

**Brain Swarms API** (`/api/brain/swarms`): `GET` returns the agent + swarm composition (handoff targets reachable via `transfer_to_brain` / `dispatch_swarm`). Used by the Agent Swarm UI tab in the extension.

## Code Review Pipeline

**Two paths available:**

1. **Legacy** (`CodeReviewService`): Hardcoded 10-step pipeline with Python `AgentSpec` definitions. Direct `POST /api/code-review/review` and `/review/stream` (SSE).

2. **PR Brain** (`PRBrainOrchestrator`): Brain-based pipeline with 4-layer prompts, per-agent `.md` identity, adversarial arbitration. Activated via `transfer_to_brain("pr_review")` from the Brain chat flow.

Both share the same post-processing code via `code_review/shared.py` (parse_findings, evidence_gate, dedup, ranking).

```
Legacy:  git diff → parse → risk → budget → impact → parallel agents → dedup → arbitration → synthesis
PR Brain: transfer_to_brain → pre-compute → dispatch agents (4-layer) → post-process → adversarial arbitration → synthesis (judge)
```

## Model A Git Workspace

```
User PAT → backend bare clone (GIT_ASKPASS) → worktree per room
  → FileSystemProvider mounts conductor://{room_id}/ in VS Code
```

## Key Patterns

### Agent Loop
```python
from app.agent_loop.service import AgentLoopService
from app.agent_loop.budget import BudgetConfig

agent = AgentLoopService(
    provider=ai_provider,
    max_iterations=25,
    budget_config=BudgetConfig(max_input_tokens=500_000),
)
result = await agent.run(query="How does auth work?", workspace_path="/path/to/ws")
# result.answer, result.context_chunks, result.tool_calls_made, result.budget_summary
```

### chat_with_tools (all 3 providers)
```python
response = provider.chat_with_tools(
    messages=[{"role": "user", "content": [{"text": "Find auth code"}]}],
    tools=TOOL_DEFINITIONS,
    system="You are a code assistant.",
)
# response.text, response.tool_calls (List[ToolCall]), response.stop_reason, response.usage
```

### Code Tools
```python
from app.code_tools.tools import execute_tool
result = execute_tool("grep", workspace="/path/to/ws", params={"pattern": "authenticate"})
# result.success, result.data, result.error
```

## Database Schema

Schema is managed by **Liquibase** (`database/changelog/`). The backend does NOT auto-create tables.

```bash
make data-up        # start Postgres + Redis
make db-update      # apply pending changesets
make db-status      # show pending changesets (dry run)
make db-rollback-one  # rollback last changeset
```

- `docker/init-db.sql` creates the `langfuse` database on first Docker start
- Langfuse manages its own tables internally (Prisma migrations)
- New changelog files go in `database/changelog/changes/` (formatted SQL)
- **Liquibase connection**: URL, username, and password are passed as `--url`, `--username`, `--password` CLI args in the Makefile (not in `liquibase.properties`). This is required because Java cannot parse bash `${VAR:-default}` syntax in JDBC URLs — use plain `${VAR}` or CLI args only.

## Chat Persistence

Chat messages use a **write-through** model:
1. Every message is written to Redis immediately (6h TTL hot cache)
2. `ChatPersistenceService` batches messages and writes to Postgres in groups of 3 (flush timer: 5s)
3. Postgres is the source of truth — history is loaded from Postgres on reconnect

Singleton services (`TODOService`, `AuditLogService`, `FileStorageService`, `ChatPersistenceService`) are initialized in `main.py` lifespan with the async SQLAlchemy engine. Do NOT call `get_instance()` without providing `engine=` on the first call.

## Testing

- `pytest` with mocked external dependencies
- `conftest.py`: stubs for cocoindex, sentence_transformers, sqlite_vec
- Code tools tests: real filesystem operations (`tmp_path` fixtures)
- Agent loop tests: `MockProvider` subclass with scripted responses
- Workflow tests: real config files from `config/`; `MockProvider` for agent execution
- ast-grep tests require `ast-grep-cli` in the venv
- tree-sitter and networkx are mocked in import stubs

### Test Files

```
tests/                                          # 1655 tests total
├── conftest.py                     # Centralized stubs + fixtures (cocoindex, etc.)
│   # Agent loop & Brain
├── test_agent_loop.py              # 55 tests — AgentLoopService, 4-layer prompt, evidence check, context clearing
├── test_agent_loop_integration.py  # Integration tests — real Bedrock models (@integration marker)
├── test_brain.py                   # 64 tests — Brain orchestrator, AgentToolExecutor, dispatch modes
├── test_mock_agent.py              # 26 tests — MockProvider scripted responses + agent harness
├── test_interactive.py             # 9 tests  — ask_user coordination (register/submit/cleanup)
├── test_prompt_builder.py          # 64 tests — 4-layer prompt assembly, skill injection, tool hints
│   # Code review
├── test_code_review.py             # 67 tests — CodeReviewService legacy pipeline
├── test_shared.py                  # 55 tests — Shared code review functions (evidence gate, dedup, ranking)
├── test_pr_brain.py                # 32 tests — PRBrainOrchestrator pipeline
│   # Code tools
├── test_code_tools.py              # 139 tests — 43 tools + dispatcher + multi-language + grep enhancements + glob + ToolMetadata
├── test_compressed_tools.py        # 24 tests — compressed_view, trace_variable, detect_patterns
├── test_detect_patterns.py         # 34 tests — detect_patterns tool (pattern extraction)
├── test_file_edit_tools.py         # 32 tests — file_edit + file_write tools
│   # Tool parity (Python ↔ TypeScript)
├── test_tool_parity.py             # 68 tests — get_dependencies/get_dependents/test_outline parity
├── test_tool_parity_ast.py         # 26 tests — AST tools parity (file_outline, find_symbol, etc.)
├── test_tool_parity_deep.py        # 34 tests — deep parity (trace_variable, compressed_view, etc.)
├── test_tool_parity_subprocess.py  # 32 tests — subprocess tools parity
├── test_local_tools_parity.py      # 23 tests — local mode tool contract validation
│   # AI providers
├── test_ai_provider.py             # 131 tests — AIProvider ABC, ClaudeDirectProvider, Bedrock, OpenAI
├── test_bedrock_tool_repair.py     # 64 tests — Bedrock tool call repair + malformed response handling
│   # Workflow + config
├── test_config_new.py              # 19 tests — Settings + Secrets YAML loading
├── test_config_paths.py            # 3 tests  — Path resolution for audit logs
├── test_style_loader.py            # 22 tests — Agent .md frontmatter + body loader
│   # Infrastructure
├── test_budget_controller.py       # 20 tests — BudgetController token accounting
├── test_session_trace.py           # 23 tests — SessionTrace (Postgres + local fallback)
├── test_evidence.py                # 19 tests — EvidenceEvaluator rule-based quality check
├── test_output_policy.py           # 21 tests — Per-tool truncation policies (budget-adaptive, glob)
├── test_symbol_role.py             # 24 tests — Symbol role extraction (AST-based)
├── test_auto_apply_policy.py       # 28 tests — Auto-apply policy enforcement
│   # Language processing
├── test_langextract.py             # 57 tests — LangExtract + multi-vendor Bedrock integration
├── test_repo_graph.py              # 67 tests — AST symbol extraction + dependency graph
│   # Chat
├── test_chat.py                    # 29 tests — WebSocket chat, identity, lead transfer
├── test_chat_persistence.py        # 16 tests — ChatPersistenceService — micro-batch Postgres writes
│   # Integrations
├── test_jira_router.py             # 45 tests — Jira OAuth 3LO + REST API router
├── test_jira_service.py            # 48 tests — JiraOAuthService token lifecycle + API calls
├── test_jira_tools.py              # 21 tests — Jira agent tools (search, create, update, get_issue)
├── test_auth.py                    # 38 tests — SSO ARN parsing, device auth flow
├── test_audit.py                   # 11 tests — AuditLogService + changeset hash
├── test_room_settings.py           # 18 tests — Room settings CRUD
│   # Git + workspace
├── test_git_workspace.py           # 75 tests — GitWorkspaceManager, worktree lifecycle
├── test_workspace_files.py         # 39 tests — workspace file browsing + filtering
├── test_db.py                      # 5 tests  — SQLAlchemy engine + table creation (Postgres)
│   # Browser + misc
├── test_browser_tools.py           # 35 tests — Browser tools (Playwright) — mocked service
└── test_main.py                    # 1 test   — FastAPI app startup / lifespan smoke test
```
