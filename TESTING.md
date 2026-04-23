# Testing Guide

[English](#english) | [中文](#中文)

---

<a name="english"></a>
## English

This guide covers running tests for the backend (Python/pytest), the extension services (TypeScript/node:test), and the React WebView (vitest).

## Backend Tests

```bash
cd backend
pytest                                        # all tests (2000+)
pytest -k "test_code_tools"                  # code tools tests only
pytest -k "test_agent_loop"                  # agent loop tests only
pytest -k "test_repo_graph"                  # repo graph tests only
pytest -k "test_scratchpad"                  # Phase 9.15 Fact Vault tests
pytest -k "test_pr_brain"                    # PR Brain orchestrator tests
pytest -k "test_dispatch"                    # dispatch_subagent + dispatch_dimension_worker
pytest -k "test_mandatory_dispatch"          # Tier 1 path + Tier 2 content mandatory detectors
pytest -v --tb=short                         # verbose with short tracebacks
pytest --cov=. --cov-report=html             # coverage report

# Tool parity (Python ↔ TypeScript)
make test-parity                              # contract check + shape validation + subprocess validation
make update-contracts                         # regenerate after schema changes
```

### Test Files

| File | Tests | Coverage |
|------|-------|----------|
| `tests/test_code_tools.py` | 139 | All 32 code tools + dispatcher + multi-language + ToolMetadata + whitespace-preservation regression (Phase 9.18 step 3) |
| `tests/test_agent_loop.py` | 55 | Agent loop + 4-layer prompt + context clearing |
| `tests/test_brain.py` | 64 | Brain orchestrator, AgentToolExecutor, 4 dispatch modes |
| `tests/test_pr_brain.py` | 81 | PRBrainOrchestrator v2 pipeline + P13 (Python/Go/Java) + P14 stub detector + Phase 2 lang hints + v2u Phase 2 reorder (P13-first + pre-verified LLM block) |
| `tests/test_dispatch_subagent.py` | 55 | `dispatch_subagent` primitive: schema validation, 7 agent_factory roles, checks/role/combined modes, depth wall, JSON parse |
| `tests/test_dispatch_dimension_worker.py` | 22 | P12b `dispatch_dimension_worker`: dimension vocab, budget floor/ceiling, trigger detector (≥3 caller files / ≥5 symbols), cap 0/1/2, executor happy path |
| `tests/test_mandatory_dispatch.py` | 55 | Tier 1 path regex detector (auth/security/crypto/migration); Tier 2 `+`-diff content scan (Java/Py/Go/TS/JS — password ==, JWT literals, whitelist/allowlist); coordinator query injection |
| `tests/test_evidence_normalization.py` | 15 | `_normalize_evidence` defensive coercion — PR #14227 char-list regression (`['@', 'V', 'a', ...]` → rejoined string) |
| `tests/test_splitter.py` | 7 | PR splitter (7.8.5) — empty diff / LLM exception / fenced output / budget truncation / user-message composition |
| `tests/test_file_edit_tools.py` | 32 | file_edit + file_write (read-before-write, staleness check, secret detection) |
| `tests/test_backend_only_tools.py` | 8 | git_hotspots / list_endpoints / extract_docstrings / db_schema smoke tests |
| `tests/test_budget_controller.py` | 20 | Token budget signals, tracking, edge cases |
| `tests/test_session_trace.py` | 23 | SessionTrace, IterationTrace, save/load |
| `tests/test_evidence.py` | 19 | Evidence evaluator (file refs, tool calls, budget checks) |
| `tests/test_symbol_role.py` | 24 | Symbol role classification + sorting + decorator detection |
| `tests/test_output_policy.py` | 21 | Per-tool truncation policies, budget adaptation, glob |
| `tests/test_compressed_tools.py` | 24 | compressed_view, module_summary, expand_symbol |
| `tests/test_detect_patterns.py` | 34 | Pattern extraction (detect_patterns tool) |
| `tests/test_langextract.py` | 57 | Bedrock provider, catalog, service, router |
| `tests/test_repo_graph.py` | 67 | Parser + graph + PageRank + RepoMapService |
| `tests/test_repo_graph_timeout.py` | 23 | Phase 9.18 step 1+2: subprocess parse pool, SIGKILL, skip_facts, JSX-depth heuristic |
| `tests/test_degraded_extraction_signal.py` | 6 | Agent-visible `extracted_via` markers on regex-fallback symbol data |
| **Scratchpad (Phase 9.15 Fact Vault)** | | |
| `tests/test_scratchpad_keys.py` | 21 | Canonical cache-key builders (24 tools), range extraction, path normalisation |
| `tests/test_scratchpad_store.py` | 22 | FactStore put/get/range_lookup, WAL concurrency, task_id meta, sweep_orphans |
| `tests/test_scratchpad_cached_executor.py` | 10 | CachedToolExecutor hit/miss, range-intersection, negative cache, skip-list |
| `tests/test_scratchpad_cli.py` | 7 | `python -m app.scratchpad` list/dump/sweep |
| `tests/test_scratchpad_search_facts.py` | 11 | search_facts tool dispatch, filters, Pydantic validation |
| `tests/test_scratchpad_inflight.py` | — | In-flight dedup via key_lock (prevents cold-cache stampede) |
| **Tool parity** | | |
| `tests/test_tool_parity.py` | 68 | get_dependencies / get_dependents / test_outline parity (direct vs TS extension) |
| `tests/test_tool_parity_ast.py` | 26 | AST tools parity (file_outline, find_symbol, find_references, get_callers, get_callees, expand_symbol) |
| `tests/test_tool_parity_deep.py` | 34 | Deep parity (trace_variable, compressed_view, module_summary, detect_patterns, test_outline) |
| `tests/test_tool_parity_subprocess.py` | 60+ | Python direct vs Python CLI shape parity. Phase 9.18 step 3 added 4 new classes: glob, ast_search, file_edit, file_write |
| `tests/test_local_tools_parity.py` | 23 | Local mode contract validation |
| **Other** | | |
| `tests/test_ai_provider.py` | 131 | All 3 AI providers + chat_with_tools + TokenUsage |
| `tests/test_bedrock_tool_repair.py` | 64 | Bedrock tool-call repair + malformed response handling |
| `tests/test_prompt_builder.py` | 64 | 4-layer prompt assembly, skill injection |
| `tests/test_shared.py` | 55 | Shared code-review functions (evidence gate, dedup, ranking) |
| `tests/test_code_review.py` | — | Shared PR review utilities (diff parser, risk classifier, dedup, ranking, PRContext) |
| `tests/test_pr_brain.py` | — | PRBrainOrchestrator v2 pipeline + P13 (Python/Go/Java) + P14 + phase-2 lang hints |
| `tests/test_dispatch_subagent.py` | — | `dispatch_subagent` primitive + 7 agent_factory role templates |
| `tests/test_auto_apply_policy.py` | 28 | Auto-apply policy enforcement |
| `tests/test_chat_persistence.py` | 16 | ChatPersistenceService: micro-batch writes, flush timer, delete room |
| `tests/test_browser_tools.py` | 35 | Browser tools (Playwright — mocked BrowserService) |
| `tests/test_git_workspace.py` | 75 | Git workspace lifecycle |
| `tests/test_chat.py` | 29 | WebSocket + history + typing indicators |
| `tests/test_jira_router.py` | 45 | Jira OAuth 3LO + REST router |
| `tests/test_jira_service.py` | 48 | JiraOAuthService token lifecycle |
| `tests/test_jira_tools.py` | 21 | Jira agent tools (search, create, update) |
| `tests/test_auth.py` | 38 | SSO ARN parsing, device auth |
| `tests/test_config_new.py` | 19 | Config + secrets |

### Code Tools Tests (139 tests)

The `test_code_tools.py` file covers all **32 code tools** using real filesystem operations via `tmp_path` fixtures. File editing tools (`file_edit`, `file_write`) have their own dedicated `test_file_edit_tools.py` (32 tests) covering read-before-write, staleness checks, secret detection, and the whitespace preservation regression (Phase 9.18 step 3: `_repair_tool_params` Pattern 3 whitelist for `file_write.content` / `file_edit.old_string` / `file_edit.new_string`):

**Basic navigation tools:**
- `grep` — regex search, multi-match, exclude patterns, binary skip
- `read_file` — full file, line range, out-of-range handling
- `list_files` — depth limits, glob filters, directory traversal
- `file_outline` — Python/JS definitions with line numbers

**Symbol discovery:**
- `find_symbol` — AST-based definition search with role classification (route_entry, business_logic, domain_model, infrastructure, utility, test)
- `find_references` — usages via grep + AST validation
- `get_dependencies` / `get_dependents` — dependency graph traversal

**Git tools:**
- `git_log` — per-file and repo-wide commit history; `search=` param filters by commit message (uses `--grep`)
- `git_diff` — diff between refs
- `git_blame` — per-line authorship (commit hash, author, date)
- `git_show` — full commit details (message + diff); also used to read pre-change file content

**Call graph tools:**
- `get_callees` — functions called within a function body
- `get_callers` — cross-file callers of a given function

**Structural search:**
- `ast_search` — ast-grep patterns (`$VAR`, `$$$MULTI`), meta-variable extraction, language auto-detection

**Test association tools:**
- `find_tests` — test functions covering a function/class (Python, Java, Go, Rust, C/C++)
- `test_outline` — test file structure with mocks, assertions, fixtures

**Data flow tracing:**
- `trace_variable` — alias detection, arg→param mapping, sink (ORM/SQL/HTTP) and source (request/annotation) patterns

**Compressed view tools:**
- `compressed_view` — file signatures + call relationships + side effects (~80% token savings)
- `module_summary` — module-level summary: services, models, controllers, functions (~95% savings)
- `expand_symbol` — expand a compressed symbol to full source (workspace-wide substring matching)

**Test execution:**
- `run_test` — execute a test file or specific test function; detect runner (pytest/jest/go test/maven/cargo); return pass/fail + output

**Dispatcher tests:**
- `execute_tool()` with unknown tool name
- Path sandboxing (escaping workspace raises `ValueError`)
- Multi-language support (Python, JavaScript, TypeScript, Java, Go, Rust, C, C++)

### Agent Loop Tests (39 tests)

The `test_agent_loop.py` file uses a `MockProvider` subclass with scripted `ToolUseResponse` sequences:

```python
class MockProvider(AIProvider):
    def __init__(self, responses: list[ToolUseResponse]):
        self._responses = iter(responses)

    def chat_with_tools(self, messages, tools, system=""):
        return next(self._responses)
```

**Coverage:**
- Single-tool-call iterations → final answer
- Multi-iteration loops with tool results injected back
- `max_iterations` termination (FORCE_CONCLUDE)
- 3-layer prompt assembly (Core Identity + Strategy + Runtime)
- Workspace reconnaissance (project marker detection)
- Accumulated text trimming (last 3 thinking turns kept)
- `AgentResult` fields: `answer`, `context_chunks`, `tool_calls_made`, `budget_summary`
- Empty answer fallback (accumulated text used when `end_turn` has no text)

### Budget Controller Tests (20 tests)

The `test_budget_controller.py` file covers `BudgetController` in `agent_loop/budget.py`:

- `NORMAL` signal — below 70% token threshold
- `WARN_CONVERGE` signal — at 70% threshold or diminishing returns detected
- `FORCE_CONCLUDE` signal — at 90% threshold or max iterations reached
- Token tracking: `track(TokenUsage)` accumulates input/output/total across all 3 providers
- Budget context injection: `get_budget_context()` returns string seen by LLM each turn
- Hard constraints at WARN: broad search tools (grep, find_symbol) blocked
- Edge cases: zero budget, negative tokens, overflow protection

### Session Trace Tests (15 tests)

The `test_session_trace.py` file covers `SessionTrace` and `IterationTrace` in `agent_loop/trace.py`:

- `IterationTrace` records: LLM latency, tool latencies, token breakdown, budget signal
- `SessionTrace` saves structured JSON to `{trace_dir}/{session_id}.json`
- Load/round-trip: saved JSON can be reloaded and compared
- Opt-in: no trace written when `trace_dir` is `None`
- Edge cases: missing directories auto-created, empty iterations saved cleanly

### Evidence Evaluator Tests (14 tests)

The `test_evidence.py` file covers `EvidenceEvaluator` in `agent_loop/evidence.py`:

- Accepts answers with ≥1 file:line reference (`path/to/file.py:42`) AND ≥2 tool calls AND ≥1 accessed file
- Rejects answers missing file references
- Rejects answers with <2 tool calls
- Rejects answers with no files accessed
- Overrides rejection when budget is exhausted (no budget remaining → accept any answer)
- Code block fallback: fenced code block counts as evidence when no file:line refs
- Budget signal integration: FORCE_CONCLUDE bypasses evidence checks

### Symbol Role Tests (24 tests)

The `test_symbol_role.py` file covers symbol role classification in `find_symbol`:

- **Tier 1 (decorator/annotation context):** `@router.get`, `@app.post`, `@Entity`, `@Controller` → `route_entry`
- **Tier 2 (file path patterns):** `models/`, `entities/`, `dto/` → `domain_model`; `tests/`, `spec/` → `test`
- **Tier 3 (name patterns):** `create_`, `update_`, `delete_` → `business_logic`; `connect_`, `send_` → `infrastructure`
- Sort order: `route_entry` > `business_logic` > `domain_model` > `infrastructure` > `utility` > `test` > `unknown`
- Multi-decorator detection (reads multiple lines above symbol)

### Output Policy Tests (19 tests)

The `test_output_policy.py` file covers `code_tools/output_policy.py`:

- `grep` — truncates by result count (default 50 matches)
- `read_file` — truncates at line boundaries (not mid-line)
- Git tools (`git_log`, `git_diff`, `git_show`, `git_blame`) — generous char limits
- `list_files` — truncates by entry count
- `compressed_view` / `module_summary` — pass-through (already compressed)
- Budget-adaptive: all limits shrink 50% when remaining tokens < 100K
- Unknown tool — default char-based truncation

### Query Classifier Tests (26 tests)

The `test_query_classifier.py` file covers `agent_loop/query_classifier.py`:

- **7 query types:** `architecture`, `bug_root_cause`, `feature_implementation`, `code_review`, `explanation`, `test_coverage`, `general`
- Keyword matching: exact phrases per type (e.g. "why does", "root cause" → `bug_root_cause`)
- LLM pre-classification: mock Haiku responses, fallback to keyword on LLM error
- Dynamic tool sets: each type gets a different 8-12 tool subset
- `filter_tools(TOOL_DEFINITIONS, tool_set)` — returns only matching tool schemas
- Strategy layer selection: each type maps to a strategy prompt module

### Compressed Tools Tests (24 tests)

The `test_compressed_tools.py` file uses real Python source files in `tmp_path`:

**`compressed_view`:**
- Function signatures with parameter types
- Class methods with signatures (not bodies)
- Detected side effects (`writes_db`, `sends_http`, `reads_file`)
- Detected raises (`ValueError`, `HTTPException`)
- Multi-language: Python, JavaScript, TypeScript

**`module_summary`:**
- Services, models, controllers, utilities classified by role
- Import list extracted
- File list enumerated
- ~95% token reduction vs reading all files

**`expand_symbol`:**
- Finds symbol by name in compressed view and returns full source
- Workspace-wide search (not limited to one file)
- Substring matching (partial names accepted)
- Returns full function/class body with surrounding context

### RepoMap Tests (72 tests)

**Parser (`parser.py`):**
- Language detection for 14 file extensions
- Regex extraction: Python functions, async functions, classes
- JavaScript/TypeScript functions, classes, interfaces
- Multiple definitions in one file
- Reference extraction
- Signature truncation for long lines
- Empty source / unknown language fallback
- `extract_definitions()` with file path and source bytes

**Graph (`graph.py`):**
- Empty workspace → empty graph
- Single file → one node
- Two files with cross-references → edge creation
- Excludes `node_modules/`, `.git/`, `venv/`
- Pre-computed symbols
- No self-edges (self-references filtered)
- Edge weight counts multiple references
- Stats dictionary populated

**PageRank (`rank_files()`):**
- Empty graph returns []
- Uniform ranking for disconnected nodes
- `top_n` limits output
- Personalised PageRank with query files
- Updates `node.pagerank` values

**RepoMapService:**
- Graph building and caching
- Force rebuild
- `generate_repo_map()` text output
- `invalidate_cache()` — specific and all
- `get_graph_stats()` — cached and uncached

### LangExtract Tests (57 tests)

The `test_langextract.py` file covers the multi-vendor Bedrock integration:

**`BedrockLanguageModel` provider (`provider.py`):**
- All Bedrock vendors via the unified Converse API (Claude, Amazon Nova, Llama, Mistral, DeepSeek, Qwen)
- `lx.extract()` call mocked; response parsing and document reconstruction
- Error handling: API exceptions, malformed responses

**`BedrockCatalog` (`catalog.py`):**
- `list_foundation_models()` → vendor grouping
- `list_inference_profiles()` → `eu.` prefix cross-region profile resolution
- `refresh()` — live discovery (mocked boto3 client)
- `get_model_ids()` — flat list for dropdown
- `models_by_vendor()` — `{"Anthropic": [...], "Amazon": [...]}`

**`LangExtractService` (`service.py`):**
- `extract_from_text()` async wrapper with `ExampleData` / `Extraction` fixtures
- `success=True` on clean extraction
- `success=False` + `error` on exception
- `list_available_models()` — delegates to catalog

**Router (`router.py`):**
- `GET /api/langextract/models` — returns vendor-grouped model list
- Catalog not attached → returns empty dict
- HTTP 200 on success

### Config Tests (27 tests)

The `test_config_new.py` file covers (RAG remnants removed):

- `CodeSearchSettings` — only `repo_map_enabled` + `repo_map_top_n`
- `AppSettings` — full model instantiation and serialization
- `load_settings()` — YAML loading, missing files, secrets merging
- JWT secrets configuration
- `setdefault()` semantics (does not overwrite existing env vars)
- AWS, OpenAI credential injection via `os.environ.setdefault()`

### Phase 9.15 + 9.18 Infrastructure (~95 tests)

The 9.15 Fact Vault + 9.18 Scan Hardening shipped an entire new layer under `app/scratchpad/` and `app/repo_graph/parse_pool.py`, backed by a matching test surface:

- **test_scratchpad_keys.py (21)** — canonical cache keys for all 24 cacheable tools, `v1:` schema prefix, path normalisation via `os.path.realpath`, pattern whitespace strip, glob-set canonicalisation
- **test_scratchpad_store.py (22)** — SQLite WAL + thread-local connections, `put`/`get` round-trip, `range_lookup` narrowest-superset preference, `put_skip` + `should_skip`, concurrent writes across threads, `task_id` in meta, `sweep_orphans(hours=24)`
- **test_scratchpad_cached_executor.py (10)** — non-cacheable passthrough, skip-list short-circuit, exact-key hit, range-intersection hit with slice, negative-cache hit, miss delegates to inner, vault write errors don't fail caller
- **test_scratchpad_cli.py (7)** — `python -m app.scratchpad list/dump/sweep` command line
- **test_scratchpad_search_facts.py (11)** — `search_facts` tool dispatch, filter combinations, Pydantic validation, no-vault-bound error shape
- **test_pr_brain.py fixture** — autouse sets `CONDUCTOR_SCRATCHPAD_ENABLED=0` so the 32 legacy pr_brain tests don't leak SQLite files into `~/.conductor/scratchpad/`
- **test_repo_graph_timeout.py (23)** — wrapper-level (mocked pool) for timeout/regex-fallback/skip-fact integration/env var; **real-subprocess tests** for actual SIGKILL + respawn behaviour; JSX-depth heuristic coverage (depth estimator counts nested components, routes large TSX to regex pre-emptively)
- **test_degraded_extraction_signal.py (6)** — `FileSymbols.extracted_via` field, `find_symbol` per-result tagging, `file_outline` dict-wrap shape change when regex fallback fires

## Extension Service Tests (node:test)

```bash
cd extension
npm test                           # 321 service tests (node:test)
npm run lint                       # ESLint check
```

### Extension Service Test Files

| File | Tests | Coverage |
|------|-------|----------|
| `xmlPromptAssembler.test.ts` | 49 | XML prompt assembly, CDATA, budget trimming |
| `relevanceRanker.test.ts` | 50 | Hybrid structural + semantic scoring |
| `conductorController.test.ts` | 35 | FSM controller + health checks |
| `conductorStateMachine.test.ts` | 33 | State transitions, listeners, serialization |
| `ssoIdentityCache.test.ts` | 30 | SSO caching, expiry, provider tracking |
| `contextPlanGenerator.test.ts` | 23 | Read ops dedup, range expansion |
| `ragClient.test.ts` | 20 | RAG HTTP client + error handling |
| `aiMessageHandlers.test.ts` | 17 | Summarize, code prompt, AI status |
| `jiraTokenStore.test.ts` | 15 | SecretStorage + file persistence |
| `projectMetadataCollector.test.ts` | 12 | Language/framework detection |
| `ticketProvider.test.ts` | 12 | Ticket key extraction + parsing |
| `jiraAuthService.test.ts` | 11 | OAuth caching, token restoration |
| `connectionDiagnostics.test.ts` | 8 | Ngrok URL selection, diagnostics |
| `backendHealthCheck.test.ts` | 6 | Health check status codes |

**Total extension service tests: 321**

## React WebView Tests (vitest)

```bash
cd extension
npm run test:webview               # 151 WebView tests (vitest + jsdom)
```

### WebView Test Files

| File | Tests | Coverage |
|------|-------|----------|
| `wsMessageParser.test.ts` | 44 | classifyMessage, parseMessageData, hasRenderableContent |
| `slashCommands.test.ts` | 25 | matchSlashCommands, computeGhostHint, parseMessageForAI |
| `chatReducer.test.ts` | 17 | ADD_MESSAGE, AI_PROGRESS, AI_DONE, CLEAR_MESSAGES |
| `components.test.tsx` | 14 | DiagramLightbox, Modal, RebuildIndexModal, CodeBlock, Toast |
| `taskBoard.test.ts` | 13 | buildDependencyGraph, todoToWorkspaceItem, jiraTicketToWorkspaceItem |
| `sessionReducer.test.ts` | 12 | SET_CONDUCTOR_STATE, SET_PERMISSIONS, SSO flow, RESET_SESSION |
| `commandTypes.test.ts` | 12 | OutgoingCommand + IncomingCommand contract validation |
| `renderMarkdown.test.ts` | 8 | HTML escaping, bold/italic/code/newline |
| `formatTimeAgo.test.ts` | 6 | Time formatting (just now, minutes, hours, days) |

**Total WebView tests: 151**

## Makefile Test Targets

```bash
make test               # ALL tests (backend + extension + webview + parity)
make test-backend       # Backend pytest only
make test-extension     # Extension service tests (node:test)
make test-webview       # React WebView tests (vitest)
make test-frontend      # Extension + WebView combined
make test-parity        # Python ↔ TypeScript tool parity
```

## CI / GitHub Actions

```yaml
# .github/workflows/test.yml (excerpt)
jobs:
  backend-tests:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: '3.11' }
      - run: cd backend && pip install -r requirements.txt && pytest

  frontend-tests:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with: { node-version: '20' }
      - run: cd extension && npm ci && npm test && npm run test:webview
```

---

<a name="中文"></a>
## 中文

本指南涵盖后端（Python/pytest）、Extension 服务（TypeScript/node:test）和 React WebView（vitest）的测试。

## 后端测试

```bash
cd backend
pytest                                        # 所有测试 (2000+)
pytest -k "test_code_tools"                  # 代码工具测试
pytest -k "test_agent_loop"                  # agent loop 测试
pytest -k "test_repo_graph"                  # repo graph 测试
pytest -k "test_scratchpad"                  # Phase 9.15 Fact Vault 测试
pytest -k "test_pr_brain"                    # PR Brain 编排器测试
pytest -v --tb=short                         # 详细输出
pytest --cov=. --cov-report=html             # 覆盖率报告

# 工具一致性验证（Python ↔ TypeScript）
make test-parity                              # 合约检查 + 形状验证 + 子进程验证
```

### 测试文件

| 文件 | 测试数 | 覆盖 |
|------|--------|------|
| `tests/test_code_tools.py` | 139 | 全部 32 个代码工具 + 调度器 + 多语言 + ToolMetadata + 空白字符保留回归（Phase 9.18 step 3）|
| `tests/test_agent_loop.py` | 55 | Agent loop + 四层提示词 + 上下文清理 |
| `tests/test_brain.py` | 64 | Brain 编排器、AgentToolExecutor、4 种分发模式 |
| `tests/test_pr_brain.py` | 81 | PRBrainOrchestrator v2 流水线 + P13（Py/Go/Java）+ P14 stub 检测 + Phase 2 语言提示 + v2u Phase 2 重排序（P13 先跑 + LLM 的 pre-verified 块）|
| `tests/test_dispatch_subagent.py` | 55 | `dispatch_subagent` 原语：schema 验证、7 个 agent_factory role、checks/role/combined 模式、depth wall、JSON 解析 |
| `tests/test_dispatch_dimension_worker.py` | 22 | P12b `dispatch_dimension_worker`：dimension 词表、预算下/上限、触发检测器（≥3 caller 文件 / ≥5 符号）、cap 0/1/2、executor happy path |
| `tests/test_mandatory_dispatch.py` | 55 | Tier 1 路径 regex 检测器（auth/security/crypto/migration）；Tier 2 `+` diff 内容扫描（Java/Py/Go/TS/JS — password ==、JWT 字面量、whitelist/allowlist）；coordinator query 注入 |
| `tests/test_evidence_normalization.py` | 15 | `_normalize_evidence` 防御性归一化 —— PR #14227 字符列表回归（`['@', 'V', 'a', ...]` → 重新拼接的字符串）|
| `tests/test_splitter.py` | 7 | PR splitter（7.8.5）—— 空 diff / LLM 异常 / 带 fence 输出 / 预算截断 / user-message 组装 |
| `tests/test_file_edit_tools.py` | 32 | file_edit + file_write（读前写、新鲜度检查、密钥检测）|
| `tests/test_backend_only_tools.py` | 8 | git_hotspots / list_endpoints / extract_docstrings / db_schema 烟测 |
| `tests/test_budget_controller.py` | 20 | Token 预算信号、跟踪、边界情况 |
| `tests/test_session_trace.py` | 23 | SessionTrace、IterationTrace、保存/加载 |
| `tests/test_evidence.py` | 19 | 证据评估器（文件引用、工具调用、预算检查）|
| `tests/test_symbol_role.py` | 24 | 符号角色分类 + 排序 + 装饰器检测 |
| `tests/test_output_policy.py` | 21 | 每工具截断策略、预算自适应、glob |
| `tests/test_compressed_tools.py` | 24 | compressed_view、module_summary、expand_symbol |
| `tests/test_detect_patterns.py` | 34 | 架构模式抽取（detect_patterns 工具）|
| `tests/test_langextract.py` | 57 | Bedrock 提供商、目录、服务、路由 |
| `tests/test_repo_graph.py` | 67 | 解析器 + 图构建 + PageRank + 服务 |
| `tests/test_repo_graph_timeout.py` | 23 | Phase 9.18 step 1+2：子进程解析池、SIGKILL、skip_facts、JSX-depth 启发式 |
| `tests/test_degraded_extraction_signal.py` | 6 | Agent 可见的 `extracted_via` 标记（regex fallback 后的符号降级信号）|
| **Scratchpad（Phase 9.15 Fact Vault）** | | |
| `tests/test_scratchpad_keys.py` | 21 | 24 个工具的规范缓存键、范围抽取、路径规范化 |
| `tests/test_scratchpad_store.py` | 22 | FactStore put/get/range_lookup、WAL 并发、task_id meta、sweep_orphans |
| `tests/test_scratchpad_cached_executor.py` | 10 | CachedToolExecutor 命中/未命中、范围交集、负缓存、skip-list |
| `tests/test_scratchpad_cli.py` | 7 | `python -m app.scratchpad` list/dump/sweep |
| `tests/test_scratchpad_search_facts.py` | 11 | search_facts 工具分发、过滤器、Pydantic 验证 |
| `tests/test_scratchpad_inflight.py` | — | key_lock 在飞去重（防止冷缓存踩踏）|
| **工具一致性（parity）** | | |
| `tests/test_tool_parity.py` | 68 | get_dependencies / get_dependents / test_outline 一致性（直接调用 vs TS 扩展）|
| `tests/test_tool_parity_ast.py` | 26 | AST 工具一致性（file_outline 等 6 个）|
| `tests/test_tool_parity_deep.py` | 34 | 深度一致性（trace_variable / compressed_view 等）|
| `tests/test_tool_parity_subprocess.py` | 60+ | Python 直调用 vs Python CLI 形状一致性。Phase 9.18 step 3 新增 4 类：glob、ast_search、file_edit、file_write |
| `tests/test_local_tools_parity.py` | 23 | 本地模式合约验证 |
| **其他** | | |
| `tests/test_ai_provider.py` | 131 | 三个 AI 提供商 + chat_with_tools + TokenUsage |
| `tests/test_bedrock_tool_repair.py` | 64 | Bedrock 工具调用修复 + 异常响应处理 |
| `tests/test_prompt_builder.py` | 64 | 4 层提示词组装、skill 注入 |
| `tests/test_shared.py` | 55 | 共享代码审查函数（证据门控、dedup、排序）|
| `tests/test_chat_persistence.py` | 16 | ChatPersistenceService micro-batch 写入、刷新计时器 |
| `tests/test_browser_tools.py` | 35 | 浏览器工具（Playwright service mocked）|
| `tests/test_git_workspace.py` | 75 | Git 工作区生命周期 |
| `tests/test_config_new.py` | 19 | 配置 + 密钥 |

### 代码工具测试要点（139 项）

- **32 个工具** 均使用真实文件系统（`tmp_path` fixture）
- grep/read_file/list_files：基础导航与正则搜索
- find_symbol：带角色分类（route_entry / business_logic / domain_model / infrastructure / utility / test）的 AST 符号查找
- get_callers / get_callees：跨文件函数调用图
- git_blame / git_show：逐行作者信息 + 完整提交详情
- find_tests / test_outline：测试关联（Python、Java、Go、Rust、C/C++）
- trace_variable：别名检测、参数→形参映射、汇聚/源模式
- compressed_view / module_summary / expand_symbol：压缩视图（80-95% token 节省）

### Agent Loop 测试要点（39 项）

- `MockProvider`：通过预设 `ToolUseResponse` 序列无需真实 LLM 即可测试
- 三层提示词组装（核心身份 + 策略层 + 运行时引导）
- 工作区侦察（项目标记检测）
- 累积文本修剪（仅保留最后 3 轮思考）
- 预算耗尽后的 FORCE_CONCLUDE 终止

### 预算控制器测试要点（20 项）

- NORMAL → WARN_CONVERGE（70% 阈值）→ FORCE_CONCLUDE（90% 或最大迭代）
- Token 跟踪：累积三个提供商的输入/输出/总计
- WARN 阶段硬约束：拒绝宽泛搜索（grep、find_symbol）
- 预算上下文字符串注入到每轮 LLM 调用

### QueryClassifier 测试要点（26 项）

- **7 种查询类型**：architecture、bug_root_cause、feature_implementation、code_review、explanation、test_coverage、general
- 关键词匹配与 LLM（Haiku）预分类
- 每种类型对应不同的 8-12 工具子集
- `filter_tools()` 工具过滤辅助函数

### LangExtract 测试要点（57 项）

- `BedrockLanguageModel`：所有 Bedrock 厂商（Claude、Nova、Llama、Mistral、DeepSeek、Qwen）
- `BedrockCatalog`：`list_foundation_models()` + `eu.` 推理配置文件解析
- `LangExtractService`：async 包装 + `ExampleData`/`Extraction` 构造
- Router：`GET /api/langextract/models` 返回厂商分组模型列表

### RepoMap 测试要点（72 项）

- 解析器：14 种文件扩展名语言检测 + 正则回退
- 图构建：跨文件引用 → 有向边 + 权重
- PageRank：均匀/个性化排名 + top_n 限制
- 服务：缓存 + 图统计

## Extension 服务测试 (node:test)

```bash
cd extension
npm test                           # 321 项服务测试
npm run lint                       # ESLint 检查
```

**总计 Extension 服务测试: 321**

## React WebView 测试 (vitest)

```bash
cd extension
npm run test:webview               # 151 项 WebView 测试（vitest + jsdom）
```

| 文件 | 测试数 | 覆盖 |
|------|--------|------|
| `wsMessageParser.test.ts` | 44 | 消息分类、解析、内容检测 |
| `slashCommands.test.ts` | 25 | 斜杠命令匹配、ghost hint、@AI 检测 |
| `chatReducer.test.ts` | 17 | ADD_MESSAGE、AI_PROGRESS、AI_DONE、CLEAR_MESSAGES |
| `components.test.tsx` | 14 | DiagramLightbox、Modal、RebuildIndexModal、CodeBlock、Toast |
| `taskBoard.test.ts` | 13 | 依赖图构建、TODO 转换、Jira ticket 转换 |
| `sessionReducer.test.ts` | 12 | 状态机、权限、SSO 流程、RESET_SESSION |
| `commandTypes.test.ts` | 12 | OutgoingCommand + IncomingCommand 契约验证 |
| `renderMarkdown.test.ts` | 8 | HTML 转义、bold/italic/code/newline |
| `formatTimeAgo.test.ts` | 6 | 时间格式化 |

**总计 WebView 测试: 151**

## Makefile 测试命令

```bash
make test               # 全部测试（后端 + Extension + WebView + 一致性）
make test-backend       # 仅后端 pytest
make test-extension     # Extension 服务测试 (node:test)
make test-webview       # React WebView 测试 (vitest)
make test-frontend      # Extension + WebView 合并
make test-parity        # Python ↔ TypeScript 工具一致性
```
