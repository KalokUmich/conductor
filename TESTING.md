# Testing Guide

[English](#english) | [中文](#中文)

---

<a name="english"></a>
## English

This guide covers running tests for the backend (Python/pytest) and the extension (TypeScript/VS Code test runner).

## Backend Tests

```bash
cd backend
pytest                                        # all tests (900+)
pytest -k "test_code_tools"                  # code tools tests only
pytest -k "test_agent_loop"                  # agent loop tests only
pytest -k "test_repo_graph"                  # repo graph tests only
pytest -v --tb=short                         # verbose with short tracebacks
pytest --cov=. --cov-report=html             # coverage report
```

### Test Files

| File | Tests | Coverage |
|------|-------|----------|
| `tests/test_code_tools.py` | 98 | All 21 code tools + dispatcher + multi-language |
| `tests/test_agent_loop.py` | 39 | Agent loop + message format + workspace layout + 3-layer prompt |
| `tests/test_budget_controller.py` | 20 | Token budget signals, tracking, edge cases |
| `tests/test_session_trace.py` | 15 | SessionTrace, IterationTrace, save/load |
| `tests/test_evidence.py` | 14 | Evidence evaluator (file refs, tool calls, budget checks) |
| `tests/test_symbol_role.py` | 24 | Symbol role classification + sorting + decorator detection |
| `tests/test_output_policy.py` | 19 | Per-tool truncation policies, budget adaptation |
| `tests/test_query_classifier.py` | 26 | Keyword + LLM classification, dynamic tool sets, filter_tools |
| `tests/test_compressed_tools.py` | 24 | compressed_view, module_summary, expand_symbol |
| `tests/test_langextract.py` | 57 | Bedrock provider, catalog, service, router |
| `tests/test_repo_graph.py` | 72 | Parser + graph + PageRank + RepoMapService |
| `tests/test_config_new.py` | 27 | Config + secrets (RAG remnants removed) |
| `tests/test_git_workspace.py` | — | Git workspace lifecycle |
| `tests/test_ai_provider.py` | 131 | All 3 AI providers + chat_with_tools + TokenUsage |
| `tests/test_chat.py` | 29 | WebSocket + history + typing indicators |

### Code Tools Tests (98 tests)

The `test_code_tools.py` file covers all **21 code tools** using real filesystem operations via `tmp_path` fixtures:

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
- `git_log` — per-file and repo-wide commit history
- `git_diff` — diff between refs
- `git_blame` — per-line authorship (commit hash, author, date)
- `git_show` — full commit details (message + diff)

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

## Extension Tests

```bash
cd extension
npm test                           # all tests (launches VS Code test host)
npm run test:unit                  # unit tests only (no VS Code)
npm run lint                       # ESLint check
```

### Extension Test Files

| File | Tests | Coverage |
|------|-------|----------|
| `src/test/sessionFSM.test.ts` | All FSM state transitions | `SessionFSM` |
| `src/test/workspaceClient.test.ts` | HTTP client methods, error handling | `WorkspaceClient` |
| `src/test/fileSystemProvider.test.ts` | read/write/delete/rename, error cases | `FileSystemProvider` |
| `src/test/workspacePanel.test.ts` | Wizard step progression, validation | `WorkspacePanel` |

**Total extension unit tests: 231**

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

  extension-tests:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with: { node-version: '20' }
      - run: cd extension && npm ci && npm test
```

---

<a name="中文"></a>
## 中文

本指南涵盖后端（Python/pytest）和扩展（TypeScript/VS Code 测试运行器）的测试。

## 后端测试

```bash
cd backend
pytest                                        # 所有测试 (900+)
pytest -k "test_code_tools"                  # 代码工具测试
pytest -k "test_agent_loop"                  # agent loop 测试
pytest -k "test_repo_graph"                  # repo graph 测试
pytest -v --tb=short                         # 详细输出
pytest --cov=. --cov-report=html             # 覆盖率报告
```

### 测试文件

| 文件 | 测试数 | 覆盖 |
|------|--------|------|
| `tests/test_code_tools.py` | 98 | 21 个代码工具 + 调度器 + 多语言 |
| `tests/test_agent_loop.py` | 39 | Agent loop + 消息格式 + 工作区侦察 + 三层提示词 |
| `tests/test_budget_controller.py` | 20 | Token 预算信号、跟踪、边界情况 |
| `tests/test_session_trace.py` | 15 | SessionTrace、IterationTrace、保存/加载 |
| `tests/test_evidence.py` | 14 | 证据评估器（文件引用、工具调用、预算检查） |
| `tests/test_symbol_role.py` | 24 | 符号角色分类 + 排序 + 装饰器检测 |
| `tests/test_output_policy.py` | 19 | 每工具截断策略、预算自适应 |
| `tests/test_query_classifier.py` | 26 | 关键词 + LLM 分类、动态工具集、filter_tools |
| `tests/test_compressed_tools.py` | 24 | compressed_view、module_summary、expand_symbol |
| `tests/test_langextract.py` | 57 | Bedrock 提供商、目录、服务、路由 |
| `tests/test_repo_graph.py` | 72 | 解析器 + 图构建 + PageRank + 服务 |
| `tests/test_config_new.py` | 27 | 配置 + 密钥（RAG 遗留代码已清除） |
| `tests/test_git_workspace.py` | — | Git 工作区生命周期 |
| `tests/test_ai_provider.py` | 131 | 三个 AI 提供商 + chat_with_tools + TokenUsage |

### 代码工具测试要点（98 项）

- **21 个工具** 均使用真实文件系统（`tmp_path` fixture）
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
