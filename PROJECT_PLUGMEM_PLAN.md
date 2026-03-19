# PlugMem Integration Plan (Revised v2)

## Context

Conductor 的 agent 每次查询从零探索 codebase，相同文件被反复 grep/read。集成 PlugMem 的三层记忆（semantic/procedural/episodic）让 agent 跨 session 累积知识。

## Key Design Challenge: Worktree + Branch Scoping

Conductor 的聊天室架构：每个 room 有独立的 git worktree + branch。

```
Room A → worktree A → branch feature/auth    → 问 "what is IDV?"
Room B → worktree B → branch main            → 问 "what is IDV?"
Room C → worktree C → branch feature/payment → 问 "how does auth work?"
```

**问题**：Room A 在 feature/auth 上学到的知识，对 Room B (main) 是否有效？

**解决方案：Repo-level 存储 + Branch-aware 检索**

知识分两类：

| 知识类型 | 跨分支共享? | 占比 | 例子 |
|---|---|---|---|
| 领域概念 (Domain) | ✅ 共享 | ~40% | "IDV = Identity Verification, 用 IDVerse" |
| 项目结构 (Architecture) | ✅ 共享 | ~15% | "业务逻辑在 backend/src/common/services/" |
| 导航策略 (Procedural) | ✅ 共享 | ~15% | "找 service 先 grep 接口，再读 Impl" |
| 具体实现 (File-level) | ❌ 需过滤 | ~30% | "auth middleware 在 new_auth.py:42 用 JWT" |

**存储**：按 `repo_url` 共享（不是按 worktree/branch 隔离）
**检索时过滤**：用 git diff 排除当前分支上已变更文件的 facts

```python
# 检索时:
changed_files = git_diff("main", current_branch)   # 当前分支相对 main 的变更
facts = SELECT * FROM memory.semantic
        WHERE repo_url = $repo AND is_active = TRUE
all_safe_facts = [f for f in facts if not overlap(f.source_files, changed_files)]
# → 领域概念（无 source_files）永远通过
# → 文件级 facts 只在未变更时通过
```

这样 knowledge graph 保持统一，同一个 repo 的所有 room 共享知识，但分支差异通过 git diff 过滤。

## Integration Strategy: Import + Adapt + Rewrite

| 类别 | 占比 | 组件 | 策略 |
|------|------|------|------|
| **直接导入** | 40% | prompt_base, value_base, value_longmemeval, prompt_reasoning, prompt_structuring, prompt_retrieving, retrieving_inference, Memory class | 复制到 `backend/app/memory/plugmem/`，不修改 |
| **写 Adapter** | 30% | get_embedding, wrapper_call_model, graph_node classes | 用 Conductor 的 AIProvider 替换硬编码的 Qwen/GPT 调用 |
| **重写存储层** | 30% | 40+ save_*/update_*/load_* 函数, build_mem_from_disk | JSON files → PostgreSQL + pgvector |

## Key Decisions

- **Embedding**: Alibaba text-embedding-v4 (1024 维, DashScope API)。部署到云后切 Bedrock Titan。
- **PostgreSQL**: 复用 Langfuse 的 langfuse-db 容器 (localhost:5433, user=langfuse, pass=langfuse-local)，创建 `memory` schema。
- **pgvector 维度**: vector(1024)。
- **Scoping**: 按 `repo_url` 存储（跨 room 共享），检索时用 `git diff main..branch` 过滤分支差异。

## Module Structure

```
backend/app/memory/
├── __init__.py
├── plugmem/                      # ← 从 PlugMem 直接导入（保留原始逻辑）
│   ├── __init__.py
│   ├── prompt_base.py            # AS-IS: PromptBase ABC
│   ├── value_base.py             # AS-IS: ValueBase ABC + scoring functions
│   ├── value_scoring.py          # AS-IS: TagEqual/Relevant, SemanticEqual/Relevant, etc.
│   ├── prompt_structuring.py     # AS-IS: GetSemanticPrompt, GetProceduralPrompt, etc.
│   ├── prompt_retrieving.py      # AS-IS: GetPlanPrompt, GetNewSemanticPrompt, etc.
│   ├── prompt_reasoning.py       # AS-IS: DefaultEpisodicPrompt, DefaultSemanticPrompt, etc.
│   ├── retrieving_inference.py   # AS-IS: get_plan, get_new_semantic, get_mode
│   ├── structuring_inference.py  # AS-IS: get_semantic, get_procedural
│   └── memory.py                 # AS-IS: Memory class (trajectory → structured knowledge)
│
├── adapters.py                   # ADAPTER: call_llm / get_embedding → Conductor AIProvider
├── graph_nodes.py                # ADAPTER: SemanticNode/TagNode/etc，去掉文件 I/O
├── db.py                         # REWRITE: asyncpg pool + schema migration + pgvector
├── repository.py                 # REWRITE: 替换 40+ save_*/load_* 函数 → SQL CRUD
├── memory_graph.py               # REWRITE: MemoryGraph 核心逻辑，pgvector 替换 numpy
├── service.py                    # NEW: MemoryService singleton (retrieve + structure_async)
├── git_invalidation.py           # NEW: branch-aware invalidation
├── models.py                     # NEW: Pydantic models
├── router.py                     # NEW: /api/memory/ endpoints
└── config.py                     # NEW: MemorySettings
```

## Database Schema (PostgreSQL + pgvector)

注意：`workspace_path` 改为 `repo_url`（repo 级共享），新增 `branch` 字段用于过滤。

```sql
CREATE EXTENSION IF NOT EXISTS vector;
CREATE SCHEMA IF NOT EXISTS memory;

-- Episodic: 原始 session 记录（按 room/branch 记录）
CREATE TABLE memory.episodic (
    id              BIGSERIAL PRIMARY KEY,
    session_id      TEXT NOT NULL,
    repo_url        TEXT NOT NULL,
    branch          TEXT NOT NULL DEFAULT 'main',
    query           TEXT NOT NULL,
    answer          TEXT,
    tool_calls_json JSONB,
    git_commit_hash TEXT,
    created_at      TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX idx_episodic_repo ON memory.episodic(repo_url);

-- Semantic: 提取的事实（repo 级共享，检索时用 source_files 做 branch 过滤）
CREATE TABLE memory.semantic (
    id              BIGSERIAL PRIMARY KEY,
    repo_url        TEXT NOT NULL,
    statement       TEXT NOT NULL,
    embedding       vector(1024),
    is_active       BOOLEAN DEFAULT TRUE,
    credibility     FLOAT DEFAULT 10.0,
    source_files    TEXT[] DEFAULT '{}',   -- 引用的文件，用于 branch diff 过滤
    source_episodic_id BIGINT REFERENCES memory.episodic(id),
    git_commit_hash TEXT,
    created_at      TIMESTAMPTZ DEFAULT now(),
    merged_into_id  BIGINT REFERENCES memory.semantic(id)
);
CREATE INDEX idx_semantic_repo_active ON memory.semantic(repo_url) WHERE is_active = TRUE;
CREATE INDEX idx_semantic_embedding ON memory.semantic
    USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

-- Tag: 内容标签（repo 级共享）
CREATE TABLE memory.tag (
    id          BIGSERIAL PRIMARY KEY,
    repo_url    TEXT NOT NULL,
    tag_text    TEXT NOT NULL,
    embedding   vector(1024),
    importance  INT DEFAULT 1,
    UNIQUE(repo_url, tag_text)
);
CREATE INDEX idx_tag_embedding ON memory.tag
    USING ivfflat (embedding vector_cosine_ops) WITH (lists = 50);

-- Semantic-Tag junction
CREATE TABLE memory.semantic_tag (
    semantic_id BIGINT REFERENCES memory.semantic(id) ON DELETE CASCADE,
    tag_id      BIGINT REFERENCES memory.tag(id) ON DELETE CASCADE,
    PRIMARY KEY (semantic_id, tag_id)
);

-- Procedural: 导航策略（repo 级共享，不引用特定文件，永远有效）
CREATE TABLE memory.procedural (
    id                  BIGSERIAL PRIMARY KEY,
    repo_url            TEXT NOT NULL,
    subgoal             TEXT NOT NULL,
    subgoal_embedding   vector(1024),
    insight             TEXT NOT NULL,
    return_score        FLOAT DEFAULT 0.0,
    source_episodic_id  BIGINT REFERENCES memory.episodic(id),
    created_at          TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX idx_procedural_subgoal_embedding ON memory.procedural
    USING ivfflat (subgoal_embedding vector_cosine_ops) WITH (lists = 50);

-- Git state: 按 repo+branch 追踪 commit（用于 main 分支上的 invalidation）
CREATE TABLE memory.git_state (
    id              BIGSERIAL PRIMARY KEY,
    repo_url        TEXT NOT NULL,
    branch          TEXT NOT NULL DEFAULT 'main',
    commit_hash     TEXT NOT NULL,
    changed_files   TEXT[] NOT NULL,
    processed_at    TIMESTAMPTZ DEFAULT now(),
    UNIQUE(repo_url, branch, commit_hash)
);
```

## Branch-Aware Retrieval

```python
# service.py → retrieve()

async def retrieve(self, query, repo_url, branch, workspace_path):
    # 1. 拿当前分支相对 main 的 changed files（用于过滤）
    branch_changed_files = _git_diff_files(workspace_path, "main", branch)

    # 2. Main 分支的 commit-level invalidation（标记过时 facts）
    await invalidate_on_main(self._pool, repo_url, workspace_path)

    # 3. pgvector 检索候选 facts（repo 级，不分 branch）
    candidates = await self._repo.search_semantic(repo_url, query_embedding, limit=20)

    # 4. Branch-aware 过滤
    safe_facts = []
    for fact in candidates:
        if not fact.source_files:
            safe_facts.append(fact)  # 领域概念，无文件引用 → 永远安全
        elif not _overlap(fact.source_files, branch_changed_files):
            safe_facts.append(fact)  # 文件在这个 branch 上没变 → 安全
        # else: 文件在这个 branch 上被修改了 → 跳过

    # 5. Tag voting + scoring（用 PlugMem 的 ValueBase）
    scored = self._score_and_rank(safe_facts, query_embedding)

    # 6. Procedural（永远共享，不需要 branch 过滤）
    procedures = await self._repo.search_procedural(repo_url, query_embedding, limit=3)

    return MemoryRetrievalResult(semantic_facts=scored[:8], procedural_insights=procedures)
```

## Git Invalidation (main 分支)

只在 main 分支上做 commit-level invalidation。Feature branches 用 diff 过滤。

```python
# git_invalidation.py
async def invalidate_on_main(pool, repo_url, workspace_path):
    """在 main 分支上检测新 commit，标记受影响 facts 为 inactive。"""
    current_hash = _git_head(workspace_path, "main")
    last_hash = await _get_last_main_commit(pool, repo_url)
    if current_hash == last_hash:
        return 0

    changed_files = _git_diff_files(workspace_path, last_hash, current_hash)
    count = await pool.fetchval("""
        UPDATE memory.semantic SET is_active = FALSE
        WHERE repo_url = $1 AND source_files && $2 AND is_active = TRUE
    """, repo_url, changed_files)
    await _record_commit(pool, repo_url, "main", current_hash, changed_files)
    return count
```

## Adapter Layer (adapters.py)

```python
_memory_provider: Optional[AIProvider] = None
_embedding_fn: Optional[Callable] = None

def call_llm(messages, temperature=0, max_tokens=4096, **kwargs) -> str:
    """Drop-in for PlugMem's call_qwen/call_gpt."""
    response = _memory_provider.chat(messages=messages, temperature=temperature, max_tokens=max_tokens)
    return response.text

async def get_embedding(text: str) -> List[float]:
    """Drop-in for PlugMem's get_embedding. Uses Alibaba text-embedding-v4."""
    return await _embedding_fn(text)
```

## Feature Gate: Zero-Intrusion Toggle

**核心原则**：业务逻辑（AgentLoopService, WorkflowEngine, CodeReviewService）不直接导入或引用 memory 模块。开关关闭时，memory 模块完全不加载，零开销。

**实现方式**：通过 FastAPI middleware + lifespan hooks 注入，不改业务代码。

```python
# memory/middleware.py — 自动注入 memory context 到 request state

class MemoryMiddleware:
    """当 memory enabled 时，在每个 agent 请求前后自动执行 retrieve / structure。

    关闭时：这个 middleware 不注册，业务逻辑完全不受影响。
    """
    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        # 在 request.state 上挂载 memory_context（如果有的话）
        # 业务代码通过 request.state.memory_context 读取（Optional, 默认 None）
        ...
```

```python
# main.py lifespan — 条件加载

if settings.memory.enabled:
    from .memory.service import MemoryService
    from .memory.middleware import MemoryMiddleware
    memory_svc = await MemoryService.create(settings.memory, resolver)
    app.add_middleware(MemoryMiddleware, memory_service=memory_svc)
    app.state.memory_service = memory_svc
    logger.info("Long-term memory: enabled (PostgreSQL)")
else:
    app.state.memory_service = None
    logger.info("Long-term memory: disabled")
```

```python
# agent_loop/service.py — 唯一的改动：从 request context 读取可选的 memory_context
# 不直接 import memory 模块

# 在 build_system_prompt() 时：
memory_context = getattr(self, '_memory_context', None) or ""
system = build_system_prompt(..., memory_context=memory_context)
```

**开关层次**：

| 层次 | 控制 | 位置 |
|------|------|------|
| YAML config | `memory.enabled: true/false` | conductor.settings.yaml |
| UI toggle | `ai-memory-toggle` | chat.html → POST /ai/memory → app.state |
| Runtime | `MemoryService.get_instance() is None` | 关闭时返回 None，所有调用方自动跳过 |

**关闭时的行为**：
- Middleware 不注册 → 零性能开销
- `app.state.memory_service = None` → 所有 `get_instance()` 返回 None
- 业务代码不 import memory 模块 → 零耦合
- PostgreSQL 连接不建立 → 零资源占用

## Agent Loop Integration (Minimal Intrusion)

业务代码不直接 import memory 模块。通过两个可选注入点集成：

**注入点 1**：`build_system_prompt()` 增加 `memory_context` 参数（默认空字符串）

```python
# prompts.py — 只加一个可选参数，不 import memory
def build_system_prompt(..., memory_context: str = ""):
    # memory_context 由调用方传入（来自 middleware 或 service 层）
    ...
```

**注入点 2**：MemoryService 提供 hooks，在 lifespan 里注册到 AgentLoopService

```python
# memory/hooks.py
class MemoryHooks:
    """Optional hooks injected into AgentLoopService at startup."""

    async def pre_query(self, query, repo_url, branch, workspace_path) -> str:
        """Returns memory context string for system prompt."""
        result = await self._service.retrieve(...)
        return _format_memory_section(result)

    async def post_query(self, trace, answer, repo_url, branch, workspace_path):
        """Fire-and-forget structuring task."""
        asyncio.create_task(self._service.structure_async(...))
```

```python
# main.py lifespan
if settings.memory.enabled:
    hooks = MemoryHooks(memory_svc)
    app.state.memory_hooks = hooks  # agent_loop/router.py 读取并传给 AgentLoopService
```

```python
# agent_loop/service.py — 构造函数加一个可选参数
class AgentLoopService:
    def __init__(self, ..., memory_hooks=None):
        self._memory_hooks = memory_hooks

    async def run_stream(self, query, workspace_path):
        # Pre-query memory retrieval (optional)
        memory_context = ""
        if self._memory_hooks:
            memory_context = await self._memory_hooks.pre_query(...)

        system = build_system_prompt(..., memory_context=memory_context)
        # ... agent loop ...

        # Post-query structuring (optional)
        if self._memory_hooks:
            await self._memory_hooks.post_query(...)
```

**这样的好处**：
- `AgentLoopService` 不 import memory 模块，只接受一个可选的 hooks 对象
- 关闭 memory 时 `memory_hooks=None`，所有 if 检查直接跳过
- 测试时可以 mock hooks，不需要 PostgreSQL

## UI (amber toggle + select in Global Model Settings)

- Toggle ID: `ai-memory-toggle`
- Select ID: `ai-memory-select`（过滤 `memory: true` 的模型）
- 颜色: amber
- Label: "Long-term Memory"
- Hint: "Learns facts from past sessions to speed up future queries"
- Backend: `POST /ai/memory` endpoint

## Files to Modify

| 文件 | 改动 |
|---|---|
| `backend/app/config.py` | 加 `MemorySettings`, `memory: bool` on AIModelConfig |
| `backend/app/main.py` | lifespan 初始化 MemoryService, 注册 router |
| `backend/app/agent_loop/prompts.py` | CORE_IDENTITY 加 `{memory_section}` |
| `backend/app/agent_loop/service.py` | pre-query retrieval + post-query structuring |
| `backend/app/agent_loop/router.py` | 传 repo_url/branch 到 service |
| `backend/app/ai_provider/router.py` | `/ai/memory` endpoint + AIStatusResponse 加 memory 字段 |
| `config/conductor.settings.yaml` | 加 `memory:` section |
| `backend/requirements.txt` | 加 `asyncpg`, `pgvector` |
| `extension/media/chat.html` | amber memory toggle+select |
| `extension/src/extension.ts` | `_handleSetMemory()` |

## Implementation Phases

| Phase | 内容 | 预计 |
|---|---|---|
| **1. Import PlugMem** | 复制 9 个文件到 `plugmem/`, 修改 import 路径 | 1天 |
| **2. Adapter Layer** | adapters.py + graph_nodes.py | 1天 |
| **3. PostgreSQL** | db.py + repository.py + schema migration | 2天 |
| **4. MemoryGraph** | memory_graph.py (pgvector + PlugMem scoring) | 2天 |
| **5. Service + Integration** | service.py + agent loop + main.py lifespan | 1天 |
| **6. Git Invalidation** | branch-aware invalidation + main commit tracking | 1天 |
| **7. UI + API** | config + router + chat.html + extension.ts | 1天 |
| **8. Testing** | unit tests + integration test + perf validation | 1天 |

## Verification

1. `@AI /ask what is IDV?` 第一次 → 7 tool calls；第二次（有 memory）→ 3-4 tool calls
2. 切到不同 branch → 文件级 facts 被 git diff 过滤，领域 facts 仍然可用
3. 同 repo 不同 room → 共享 knowledge graph
4. Langfuse trace 对比：token 消耗下降 30-50%
5. `GET /api/memory/stats` 返回 fact/procedure 计数
6. Git invalidation：main 上 commit → 相关 facts inactive
7. UI：toggle memory on/off，选择 model
