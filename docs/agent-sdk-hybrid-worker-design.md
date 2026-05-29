# Design: Hybrid SDK Worker — Claude Agent SDK as the worker inner-loop

> Status: **DRAFT for review** · Last updated: 2026-05-29 · Author: kalok (+ Claude)
> Baseline commit: `77497d1` (origin/main). All file:line refs are against this commit.
>
> **How to use this doc at the office:** read it top to bottom, then jump to
> §9 (Open decisions) — those are what we need to settle before any code.
> This doc supersedes the earlier `agent-sdk-migration-discussion.md` (which
> was written against a stale fork and assumed things that are no longer true).

---

## 0. TL;DR

- We are **NOT** replacing the in-house agent loop / Brain wholesale. The
  mainline already chose a different path (ADR-016): study Claude Code's
  source and **port patterns** into our own stack, not adopt the framework.
- The one genuinely clean, high-value insertion is narrow: **let the Claude
  Agent SDK own the inner loop of a *dispatched worker*** (the thing
  `dispatch_explore` / `dispatch_verify` / `dispatch_sweep` spawns), while the
  **coordinator (domain orchestration) stays ours**.
- This must be **hybrid**, because our explorer tier is deliberately
  multi-vendor (Haiku / DeepSeek / Qwen / Nova on Bedrock). The SDK speaks
  Anthropic protocol only. So: **Claude workers → SDK; non-Claude workers →
  existing `AgentLoopService`.** Branch point is one line: `brain.py:1323`.
- Recon says the seam is clean: ~200–300 LOC + minor refactor. The real cost
  is **maintaining two worker execution paths forever**, not the initial build.

---

## 1. Why this, why now

"Opus / the SDK keep getting better at multi-agent" is true, and the generic
agent loop (iterate → call tools → compact context → repeat) is exactly the
kind of commodity machinery Anthropic out-iterates us on. Maintaining our own
is paying a perpetual "catch-up tax" on something that isn't our moat.

Our moat is **domain orchestration** (PR Brain v2's deterministic pipeline),
**local/native tools**, and **memory** (Fact Vault). Those we keep and invest
in. The generic worker loop is the part worth handing off — *for the models
the SDK actually supports*.

---

## 2. What the mainline already decided (context we must respect)

Two formal ADRs in `ROADMAP.md` set the direction:

- **ADR-014** — agent loop over RAG for code context (why the loop exists).
- **ADR-016** — use Claude Code source (`reference/claude-code/`, ~205K LOC TS,
  extracted 2026-03-31) as the **primary architecture reference**: cherry-pick
  patterns into our Python/TS stack. `reference/CLAUDE.md` lists 10 patterns to
  port (loop recovery, streaming tool exec, prompt-cache sharing, Dream memory,
  hook system, coordinator mode, …).

Already shipped from that effort (these are the patterns, already real code):
- `forked.py` `fork_call` — prompt-cache-stable prefix reuse (~90% verifier
  input-cost cut). = Claude Code's "fork agent".
- `lifecycle.py` — 4 Brain lifecycle hooks. = hook event system.
- `scratchpad/` — Fact Vault (per-session SQLite, range-intersection dedup,
  `search_facts`, `update_notes`). = coordinator shared scratchpad.
- `dispatch_explore / dispatch_verify / dispatch_sweep` — coordinator-worker.

**Implication:** This hybrid-SDK-worker idea is a *deliberate, scoped* reversal
of ADR-016 — but only for the worker inner-core of Claude workers, not for the
coordinator and not for non-Claude workers. It must be framed that way to the
team, not as "let's adopt the SDK."

---

## 3. Current architecture (the parts this touches)

### 3.1 Coordinator → worker dispatch

`backend/app/agent_loop/brain.py`, class `AgentToolExecutor`:
- `_dispatch_explore` (≈1249–1413) — open-ended investigation worker.
- `_dispatch_verify` (≈671–1012) — scope-bounded, 3 falsifiable checks;
  internally funnels into `_dispatch_explore`.
- `_dispatch_sweep` (≈1014–1247) — full-diff single-lens; also funnels in.
- All three converge on **one** `AgentLoopService(...)` construction at
  **`brain.py:1372–1392`**, with provider chosen at **`brain.py:1323–1324`**:
  ```python
  provider = self._strong_provider if resolved_model == "strong" else self._agent_provider
  ```
  This line is the hybrid branch point.

### 3.2 The worker (generic loop — the replaceable part)

`backend/app/agent_loop/service.py`, `AgentLoopService`:
- Loop-internal machinery the SDK would *replace*: budget tracking
  (`budget.py`, instantiated ~300), context clearing
  (`_clear_old_tool_results`, called ~366, impl ~1818), evidence retry
  (`_handle_final_answer` ~466, `evidence.py`), SessionTrace (~251).
- The Brain does **not** depend on any of these objects directly — it only
  reads the returned result (see 3.4). So the SDK path may implement its own
  or skip them.

### 3.3 Reusable, already-decoupled pieces (good news)

- **4-layer prompt** = pure function `build_sub_agent_system_prompt(...)`
  (`prompts.py:1320–1436`) → returns a plain string. Called inside the loop
  today (`service.py:698`) but has zero coupling to loop internals. **Both
  paths call the same function.**
- **Tool executor** = `ToolExecutor` ABC; workers receive a
  `CachedToolExecutor` (Fact Vault wrapper) via the `tool_executor=` param.
  Wrapping happens **outside** the loop, in `PRBrainOrchestrator.__init__`
  (`pr_brain.py:656`): `self._tool_executor = CachedToolExecutor(tool_executor, scratchpad)`.
  **Caching lives at the tool-call layer, not the loop layer** — so if the SDK
  owns the loop and calls tools through this same executor, vault hits still
  work (see §6 for the catch).

### 3.4 The worker return contract (the cross-path seam)

`AgentResult` (`service.py:100–116`): `answer`, `context_chunks`,
`thinking_steps`, `tool_calls_made`, `iterations`, `duration_ms`, `error`,
`budget_summary`, `files_accessed`.

The coordinator consumes it via `condense_result()` (`brain.py:382–393`), which
is **duck-typed** (`getattr/.get/hasattr`). So the SDK path only needs to
return an object/dict that *looks like* `AgentResult` on those ~10 fields. The
contract is narrow and forgiving.

---

## 4. The hard constraint that forces "hybrid"

The explorer tier is intentionally multi-vendor. `config/conductor.settings.yaml`
enables, with `explorer: true`, models across **Claude (Haiku/Sonnet/Opus on
Bedrock EU), Qwen, DeepSeek V3.2, Amazon Nova, Mistral, Kimi, Nemotron, GLM-5**.
This is a strategic anti-lock-in stance, not legacy cruft. (LiteLLM was removed
in `09a364e`; there is no gateway in the path today.)

**The Claude Agent SDK speaks the Anthropic Messages protocol only.** Verified
SDK facts (Python `claude-agent-sdk`):

| Capability | SDK support | Mechanism |
|---|---|---|
| Per-worker **model** | ✅ | `ClaudeAgentOptions(model=...)` per query/client |
| Only our 46 tools, built-ins off | ✅ | `create_sdk_mcp_server` + `@tool`; `tools=[]` disables built-ins |
| Tools that proxy WebSocket + caching | ✅ | SDK sees them as plain async fns |
| Structured/typed worker result | ✅ | `output_format` + Pydantic, auto-retry |
| Per-worker **api_key / base_url** | ❌ | `ANTHROPIC_API_KEY` / `ANTHROPIC_BASE_URL` read from **process env once**; no per-call/per-client field |
| Non-Claude model in-process | ❌ | only via `ANTHROPIC_BASE_URL`→ gateway (LiteLLM); loses prompt caching / extended thinking / fine-grained tool streaming |

**Consequence:** You can switch *model* freely among Claude models in one
process (shared Bedrock key). You **cannot** have worker A → Bedrock Claude and
worker B → Qwen-via-gateway in the **same process**, because base_url is
process-global. Running Qwen under the SDK would require **subprocess isolation
per non-Claude worker + a gateway**, *and* you'd lose the very SDK benefits you
adopted it for. Not worth it.

→ Therefore: **don't run non-Claude under the SDK at all.** Claude workers →
SDK; non-Claude workers → existing `AgentLoopService` (already multi-vendor).

---

## 5. Proposed design

### 5.1 Shape

```
Coordinator (ours — PR Brain v2, unchanged)
  │  dispatch_explore / _verify / _sweep
  │  brain.py:1323 — choose execution backend by provider vendor
  ├── is_claude(provider)  → SdkWorkerRunner   (NEW; SDK owns loop)
  └── else                 → AgentLoopService   (existing; multi-vendor)
        both → return AgentResult-shaped object → condense_result() (unchanged)
```

### 5.2 The branch (brain.py:1323)

```python
provider = self._strong_provider if resolved_model == "strong" else self._agent_provider

if is_claude_provider(provider):          # NEW
    result = await run_sdk_worker(
        provider=provider,                # gives model id; key/base_url from env
        system_prompt=system_prompt,      # build_sub_agent_system_prompt(...) — shared
        user_message=query,
        tool_executor=sub_executor,       # SAME CachedToolExecutor instance
        budget_tokens=budget_tokens,
        output_schema=AGENT_FINDINGS_SCHEMA,
    )
else:
    svc = AgentLoopService(provider=provider, config=AgentLoopConfig(...),
                           tool_executor=sub_executor, ...)   # unchanged
    result = await svc.run(...)
```

### 5.3 New component: `SdkWorkerRunner` (~200–300 LOC)

Responsibilities:
1. Build an in-process MCP server exposing our 46 tools — each `@tool` handler
   delegates to the **same `CachedToolExecutor`** so Fact Vault still hits.
2. `ClaudeAgentOptions(model=<from provider>, mcp_servers={...},
   allowed_tools=[mcp__conductor__*], tools=[], output_format=<schema>)`.
3. Run the SDK query; collect tool-call count, files touched, token usage.
4. Map SDK result → an `AgentResult`-shaped object for `condense_result()`.

### 5.4 What both paths must honor (the real, permanent contract)

1. **Prompt**: both call `build_sub_agent_system_prompt()` (already shared).
2. **Tools**: both drive tool calls through the same `CachedToolExecutor`
   instance (vault parity).
3. **Return shape**: both produce the ~10 `AgentResult` fields (duck-typed).
4. **Token reporting**: both populate `budget_summary` so the coordinator's
   budget view is consistent.
5. **Concurrency**: both respect the Brain's `llm_semaphore` gating.

These 5 contracts are the maintenance burden of the hybrid — every future
change to worker behavior must be made/verified in both paths.

---

## 5.5 Tools as a remote-execution proxy — why swapping the engine doesn't break local mode

**The single most valuable abstraction in our architecture: the agent engine
never touches files. Tools do.** The engine only ever *calls a tool*; where
that tool runs and whose disk it reads is the tool's concern, not the engine's.
This decouples the agent engine (in-house **or** Claude Code) from where the
code physically lives (backend disk **or** the user's machine).

### 5.5.1 The mechanism is unchanged — only the caller's name changes

Today (in-house loop, local mode):
```
backend agent → "read file X" → RemoteToolExecutor → WebSocket →
  extension runs the TS tool on the USER's machine → result back to agent
```

With a Claude Code (SDK) worker, local mode:
```
Claude Code (backend) calls mcp__conductor__read_file
  → our @tool handler (backend) — this IS the WebSocket bridge
    → WebSocket → extension runs TS tool on user's machine → result
  → handler returns to Claude Code
```

The `@tool` handler simply *is* today's `RemoteToolExecutor` in a new wrapper.
**Claude Code neither knows nor needs to know the file lives on the user's
machine.** So local "explain this code" works fine under a Claude Code worker —
the earlier worry that "the backend Claude Code can't see local files" was a
non-issue: it never needed to see them, it calls a tool.

### 5.5.2 The one hard rule this creates

The bridge trick works for **our MCP tools** (their handlers can be redirected
over WebSocket). It does **not** work for Claude Code's **built-in** tools
(`Read` / `Grep` / `Bash`) — those execute in the SDK's own process and read
the **backend** disk; they cannot be redirected to the user's machine.

→ **Rule: in local mode, do not enable Claude Code's built-in tools. Use only
our MCP tools (which proxy over WebSocket).** This splits the worker tool
strategy by path:

| Path | Where code lives | Claude Code built-in tools? | Tool strategy |
|---|---|---|---|
| **PR review** (backend Model A worktree) | backend disk | ✅ yes — free Read/Grep/Bash + auto-loads CLAUDE.md | **C**: built-ins + our unique MCP tools (search_facts, compressed_view, …) |
| **Local interactive** ("explain this code") | user's machine | ❌ no — would read the wrong disk | **B**: 100% our MCP tools (WebSocket-proxied) |

### 5.5.3 Why local mode (strategy B) is not a loss

In local mode Claude Code contributes only **loop + strong model + harness**;
tools are 100% ours. That's fine:
- Our 46 TS tools are parity-tested and high quality (rg-based grep,
  tree-sitter AST) — not worse than the built-ins.
- CLAUDE.md auto-loading is unavailable on the backend in local mode anyway, but
  we already inject project docs via the 4-layer prompt's `project_docs` — the
  guideline channel exists regardless.

So local mode becomes: **"Claude Code's brain (loop + model) driving our hands
(46 proxied tools)."** Fully self-consistent.

### 5.5.4 Honest residual risk (spike-measurable, not a blocker)

Claude Code's model has the strongest "muscle memory" for its **own** built-in
tools (it's trained heavily on Read/Grep/Bash). When local mode forces it onto
custom MCP tools (`mcp__conductor__read_file`, …), it will still use them, but
fluency *may* be marginally lower than with native tools. Likely small (our tool
names/semantics are conventional), but **measure it in the spike**: compare
exploration quality of local mode (all-MCP) vs PR-review path (built-ins
allowed) on the same eval cases, confirm the gap is acceptable.

### 5.5.5 Consequence for the plan

- The local interactive path needs **no special handling** and must **not** be
  dropped — it runs on the same SDK-worker mechanism, just with tool strategy B
  instead of C.
- Our WebSocket proxy + local TS tools are **not** in competition with adopting
  Claude Code workers — they are the **prerequisite** that makes local mode work
  under the new engine. "Keep local TS tools" and "adopt Claude Code workers"
  are complementary, not either/or.

---

## 5.6 Harness prompt engineering — what we inherit vs. what stays our job

A common (and load-bearing) question: *"Claude Code's harness prompt
engineering is clearly very tuned — does the SDK give us that, or do we
re-implement it?"* The trap is treating "harness prompt engineering" as one
blob. It's **three layers** with **completely different ownership**. Two are
free; one is permanently ours.

| Layer | What it is | SDK gives us? |
|---|---|---|
| **① Engine-internal prompting** | How the loop orchestrates tool use, how tool results are formatted, error rendering, thinking blocks, parallel read-only tool execution, user/assistant scaffolding | ✅ **Inherited automatically, inseparable** — it's engine behavior, not a prompt file. Using the SDK = you have it, identical to the CLI. |
| **② Claude Code's flagship system prompt + built-in tool descriptions** | The heavily-tuned agent persona / coding guidelines / response style, and the precision-engineered descriptions for Read / Grep / Bash etc. | ✅ **Available, but opt-in via one switch** (see below). |
| **③ Our own tool descriptions + agent identities/roles** | The `@tool` descriptions for our 46 tools; our agent roles (pr_security, correctness, …); our 4-layer prompt | ❌ **Always ours. This is our IP — the SDK can't and shouldn't do it for us.** |

**The reframe:** what you actually envy about Claude Code's harness is mostly
**layer ①** (loop behavior) and **layer ②** (the tuned system prompt). Both are
**inherited, not re-implemented**. So "can the SDK do Claude Code's prompt
engineering?" → you don't implement it, you *inherit* it. The funds you save by
adopting the SDK are exactly this catch-up work. Layer ③ was always your job and
you're already doing it to Anthropic's own best-practice spec (CLAUDE.md's
4-layer rule, 3-4-sentence tool descriptions).

### 5.6.1 Layer ② is a one-line opt-in (the pleasant surprise)

The SDK's **default** system prompt is minimal (just enough to call tools). To
inherit Claude Code's full, tuned system prompt — and *append* your own on top
rather than replace it:

```python
ClaudeAgentOptions(
    system_prompt={
        "type": "preset",
        "preset": "claude_code",        # inherit Anthropic's tuned agent prompt
        "append": "<our domain role / 4-layer identity goes here>",
    }
)
```

- `preset: "claude_code"` → inherit the flagship agent prompt.
- `append` → **stack our role on top of Anthropic's tuned base** (not replace).
  Net effect: "Anthropic's tuned agent persona + our domain specialization."
- Built-in tools (Read/Grep/Bash), when enabled, carry their tuned descriptions
  for free.
- `exclude_dynamic_sections: True` (Python ≥0.1.58) moves per-session context
  (cwd/git/OS) into the first user message so the system prompt stays
  cache-stable across workers — relevant for our prompt-cache economics.

### 5.6.2 The honest reverse caveat — what is NOT inherited

Two things the SDK does **not** do automatically happen to be things our
in-house engine already does and our domain pipeline needs:

1. **Context compaction.** Our "clear old tool results after 3 turns"
   (`service.py:366`) is **not** auto-done by the SDK — it keeps full history.
   To replicate, implement it via a `PostToolUse` hook or session management.
2. **Mid-loop system-reminder injection.** The SDK injects only the initial
   system prompt; it does not push mid-conversation reminders. If a worker
   relies on that, re-add it via hooks.

Consistent with the whole design: **generic behavior is inherited; our
domain-specific control (compaction policy, reminders) must be re-attached via
the SDK's hook system.**

### 5.6.3 Tool-affinity caveat (ties to R7)

The model has training affinity for the **built-in** tool names (Read/Grep/Bash)
that custom `mcp__conductor__*` tools do **not** get. Custom tools compete on
**description quality alone** — which raises the stakes on layer ③ and is
exactly why R7 (local-mode all-MCP exploration quality) must be measured.

---

## 5.7 Full-codebase refactor ledger — going Bedrock-only + Claude-only + SDK

> Source: a full read-only sweep of the repo (2026-05-29) covering the tool set,
> the multi-vendor coupling, the observability/token/COT stack, and verified SDK
> capabilities. This section answers: *if we abandon multi-vendor and commit to
> Bedrock+Claude+SDK, what changes?*

**Headline:** this is a **net ~5,200 LOC reduction** (≈6,700 deleted, ≈500
refactored, ≈1,000 new). A large fraction of our "multi-vendor abstraction" is
really **non-Claude compatibility patching** that simply evaporates.

### 5.7.1 Provider layer — mostly deletion

| Component | File | Action | Why |
|---|---|---|---|
| OpenAI provider | `ai_provider/openai_provider.py` (~430) | **DELETE** | OpenAI/Alibaba/Moonshot all routed through it |
| Two-stage summary | `ai_provider/pipeline.py` + `prompts.py` (~800) | **DELETE** | Summarization becomes a Brain/agent job |
| Tool-repair pipeline | `claude_bedrock.py:152–432` (`_repair_tool_calls`, `_parse_malformed_name`, `_extract_kv_pairs`, `_extract_xml_tool_calls`, `_extract_tool_calls_from_text`) (~500) | **DELETE** | These exist to fix **non-Claude** Bedrock models' malformed tool calls. Claude-only → dead. |
| Schema sanitization | `claude_bedrock.py:41–93` (`_sanitize_schema`/`_sanitize_property`) | **DELETE/keep-tiny** | SDK handles schema natively |
| Converse call surface | `claude_bedrock.py` (`client.converse(...)`) | **DELETE** | SDK uses native Anthropic-on-Bedrock, not Converse |
| Resolver | `resolver.py` 5-ProviderType enum, health checks, `enable_thinking`, per-vendor `_create_provider` | **SIMPLIFY** | Collapses to Bedrock(+optional Anthropic-direct fallback) |
| Config | `conductor.settings.yaml` 19 models/5 providers; `config.py` OpenAI/Alibaba/Moonshot SecretsConfig + env vars | **SIMPLIFY** | → 4 models / 2 providers; delete 3 secrets classes + ~5 env vars |
| langextract | `langextract/provider.py` multi-vendor Bedrock routing (qwen/llama/mistral/nova/deepseek regex) | **SIMPLIFY** | Claude-on-Bedrock only |
| Tests | `test_bedrock_tool_repair.py` (945, **DELETE**), `test_ai_provider.py` (~70% del), `test_langextract.py` (~70% del) | **DELETE/REWRITE** | ~2,900 test LOC gone |

**Kept (reframed):** `base.py` TokenUsage/ToolCall (reused for observability),
`prompt_builder.py` (template util, no API call), `claude_direct.py` (optional
Anthropic-direct failover only).

### 5.7.2 Tool set — 51 tools, only 6 overlap, none trivially deletable

Sweep count: **6 DUPLICATE / 15 UNIQUE-AST / 30 UNIQUE-DOMAIN** (51 total).

- **6 DUPLICATE** (`grep`, `read_file`, `file_edit`, `file_write`, `run_test`,
  `web_search`) — Claude Code has built-ins. On the **PR-review/backend** path we
  *can* use built-ins; but the **TS versions must stay for local mode** (built-ins
  read backend disk only — see §5.5). So these are **"built-in on backend, keep
  TS for local"**, not "delete".
- **15 UNIQUE-AST** (`find_symbol`, `find_references`, `file_outline`,
  `get_callers`, `get_callees`, `get_dependencies`, `get_dependents`,
  `trace_variable`, `compressed_view`, `expand_symbol`, `ast_search`,
  `detect_patterns`, `module_summary`, `test_outline`, `git_hotspots`) — **no SDK
  equivalent; this is the differentiation.** Keep as MCP, both Py + TS.
- **30 UNIQUE-DOMAIN** (Jira×5, browser×6, Brain dispatch×5+, Fact Vault
  `search_facts`/`update_notes`, git×5, `list_endpoints`, `extract_docstrings`,
  `db_schema`, `find_tests`, `ask_user`, `signal_blocker`) — **no SDK
  equivalent.** Keep as MCP (backend).

**Conclusion on "some tools duplicate Claude's":** true but only 6, and the
local-mode constraint means the win is "stop maintaining the *backend Python*
version of those 6," not "delete the tool." Real tool-set simplification is
modest; the AST + domain tools are exactly our moat.

### 5.7.3 Caching — Fact Vault stays; it composes with SDK prompt caching

- **Keep** `scratchpad/` Fact Vault (per-session SQLite, range-intersection
  dedup, negative cache). SDK has **no equivalent** — it's domain optimization.
- It lives at the **tool-call layer** (`CachedToolExecutor`), so SDK workers keep
  hits **iff** every `@tool` handler routes through it (R1).
- **Two different cache layers, complementary:** SDK **prompt caching**
  (`cache_read/cache_write` tokens) caches *prompt tokens*; Fact Vault caches
  *tool results*. They stack. The manual cache-prefix trick in `fork_call`
  (`build_pr_context_prefix`) can be partly handed to SDK-native prompt caching,
  simplifying it.

### 5.7.4 Token + COT tracking — SDK is an upgrade, Postgres stays for audit

Current state (verified):
- COT today is **reconstructed** — `ThinkingStep` built from tool calls + the
  LLM's text truncated to 500–1000 chars (`service.py`, `trace.py`). Not real
  model reasoning.
- Token usage normalized into `TokenUsage` (`base.py`); per-session totals in
  Postgres `session_traces` (`db/models.py:66–84`,
  `database/changelog/changes/001-initial-schema.sql:14–31`); per-iteration in
  `trace_json`. **Cache tokens go only to Langfuse, never persisted.**

With the SDK:
- **Real COT** — enable `thinking`; `AssistantMessage.content` carries structured
  `ThinkingBlock` (full reasoning + signature). Upgrade over our reconstruction.
- **Full structured transcript** — every assistant turn / tool_use / tool_result
  as typed objects, directly persistable.
- **Better usage** — `message.usage` (input/output/cache_read/cache_creation) +
  `ResultMessage.model_usage` (per-model). Lets us finally persist cache tokens.
- **Still our job** — cross-session cumulative totals + queryable historical
  audit. So: **keep the Postgres `session_traces` table**, but (a) swap the COT
  field from reconstruction → SDK `ThinkingBlock`, (b) start persisting cache
  tokens. `total_cost_usd` from the SDK is a local estimate (drifts) — for real
  billing use Anthropic's Usage & Cost API, not the SDK number.

### 5.7.5 Langfuse — replaceable by SDK-native OTEL, not by the SDK itself

- Today: Langfuse default **off** (`config.py` `enabled: false`), wired via
  `workflow/observability.py` `@observe` + `track_generation`. It and SessionTrace
  **already overlap** on token tracking.
- SDK/Claude Code has **native OpenTelemetry export**
  (`CLAUDE_CODE_ENABLE_TELEMETRY=1` + `OTEL_*` exporters): tokens, cost, latency,
  tool spans, `session.id`. Langfuse can **ingest OTLP**.
- → **We can retire our hand-wired `@observe`/`track_generation` and emit OTEL
  instead.** Whether to keep a Langfuse UI on the OTLP stream is a *separate*
  choice (LLM trace visualization still has value; not required).
- ⚠️ Caveat: SDK OTEL **traces are beta** (span names may change); content
  (prompts/tool IO) is off unless opted in (`OTEL_LOG_*`).

### 5.7.6 Refactor sequencing (safe → risky)

1. **Config cleanup** — drop non-Claude models/providers/secrets/env (zero
   behavior change for Claude path).
2. **Provider dead-code removal** — delete OpenAI provider, tool-repair, schema
   sanitization, `enable_thinking`; simplify resolver.
3. **Feature deprecation** — delete two-stage summary pipeline + `/ai/summarize`.
4. **SDK integration (needs spike first)** — `SdkWorkerRunner` behind
   `brain.py:1323`; route `@tool` through `CachedToolExecutor`.
5. **Observability swap** — SDK `ThinkingBlock` → Postgres COT; enable OTEL;
   retire `@observe` wiring.
6. **Test rewrite** — delete `test_bedrock_tool_repair.py`, trim
   `test_ai_provider.py` / `test_langextract.py`; add SDK-worker tests.

### 5.7.7 What this section does NOT recommend

This ledger describes the *Bedrock-only + Claude-only* end state **if** decision
D1 (and the explorer-tier-goes-Claude call) is taken. It is the maximal-cleanup
scenario. If the team keeps non-Claude explorers, the hybrid (§5) still holds but
most of §5.7.1's deletions do **not** apply — the provider layer stays. **Don't
execute 5.7.1 until the explorer-tier decision is settled.**

---

## 6. Risks & open technical questions (de-risk before committing)

- **R1 — Fact Vault at the tool boundary.** Caching is at the tool-call layer
  (`CachedToolExecutor.execute`), so in principle the SDK path keeps vault hits
  *if* every SDK `@tool` handler routes through that executor. **Must verify**:
  the SDK calls each tool through our handler exactly once per call, with
  params in the shape `build_key()` expects (`scratchpad/keys.py`). Spike seam.
- **R2 — Local TS tools over WebSocket.** A subset of the 46 tools proxy to the
  VS Code extension over WebSocket. Wrapping them as SDK `@tool` handlers is the
  original spike seam #1. Must prove one end-to-end.
- **R3 — Context compaction divergence.** SDK owns compaction for SDK workers;
  `AgentLoopService` does its own 3-turn clearing for non-Claude. Behavior will
  differ between the two paths — acceptable, but eval must cover both.
- **R4 — Prompt-cache benefit.** A real win only materializes if the SDK worker
  reuses a cache-stable prefix (like `build_pr_context_prefix` in `forked.py`).
  Design the system prompt prefix to be cache-stable across a PR's workers.
- **R5 — Streaming/trace parity.** SessionTrace + Langfuse currently fed from
  inside `AgentLoopService`. SDK path needs an equivalent or we lose
  observability for SDK workers. Decide: replicate, or accept reduced trace.
- **R6 — Two-path drift.** The permanent cost. Mitigate by keeping the SDK path
  thin and pushing shared logic into the 5 contracts above.
- **R7 — Native-tool fluency in local mode.** Model has affinity for built-in
  tool names; custom MCP tools compete on description alone. Measure (§5.5.4).
- **R8 — Cache-token persistence gap.** Today cache tokens reach only Langfuse,
  never Postgres (§5.7.4). The SDK exposes them per message — persist on migration
  so we don't keep losing cache-efficiency history.
- **R9 — SDK OTEL traces are beta.** Span names/attributes may change; content is
  opt-in. Pin the beta flag and re-check on SDK upgrades before relying on it to
  replace Langfuse wiring (§5.7.5).

---

## 7. De-risking spike (1 week, before any real build)

Prove the three seams; any one failing changes the plan:
1. **One local TS tool as an SDK `@tool`** — runs through WebSocket proxy,
   returns identical output to today. (R2)
2. **CachedToolExecutor behind an SDK tool** — drive a read_file twice, confirm
   the second is a vault hit; confirm range-intersection still works. (R1)
3. **SDK worker model-switch + result mapping** — one query on Haiku-Bedrock,
   one on Sonnet-Bedrock, both returning a Pydantic-validated `AgentFindings`
   that `condense_result()` accepts unchanged. (return contract)
4. **Local-mode all-MCP exploration quality** — run a Claude Code worker with
   built-ins disabled, all tools proxied over WebSocket; compare exploration
   quality vs the PR-review path (built-ins allowed) on the same eval cases.
   Confirm the native-tool-fluency gap (§5.5.4) is acceptable. (R7)

Acceptance: a Claude worker dispatched via `brain.py:1323` runs on the SDK,
uses only our tools, hits the vault, and the coordinator can't tell the
difference from an `AgentLoopService` worker.

---

## 8. Out of scope (explicitly NOT doing)

- Replacing the coordinator / PR Brain v2 pipeline with the SDK.
- Running non-Claude models under the SDK (stays on `AgentLoopService`).
- Adopting the Claude Code **CLI** as a subprocess (we use the **SDK** library).
- Building a request-level failover gateway (separate, independent concern —
  noted in §10).

---

## 9. Open decisions (settle these at the office)

- **D1 — Go/no-go on the hybrid**, given the permanent two-path cost (§5.4).
  Alternative: stay pure ADR-016 (port SDK loop *ideas* into AgentLoopService,
  no framework dependency at all).
- **D2 — Scope of phase 1**: Claude workers only first (lowest risk), or build
  the full branch with both paths from the start?
- **D3 — Observability**: replicate SessionTrace/Langfuse in the SDK path (R5),
  or accept reduced tracing for SDK workers initially?
- **D4 — Which dispatch first**: `dispatch_explore` (simplest) vs the verifier
  path (already `fork_call`, arguably the SDK's sweet spot)?
- **D5 — Maximal cleanup or not** (gated on the explorer-tier-goes-Claude call):
  if yes, execute the §5.7 ledger (~5,200 LOC net deletion: drop OpenAI provider,
  tool-repair, Converse, summary pipeline, trim config/tests). If explorers stay
  multi-vendor, keep the provider layer and only add the SDK path.
- **D6 — Observability target**: retire hand-wired Langfuse `@observe` in favor
  of SDK-native OTEL (§5.7.5)? Keep a Langfuse UI on the OTLP stream or not? And
  persist cache tokens + swap COT to SDK `ThinkingBlock` in `session_traces`
  (§5.7.4)?

## 10. Adjacent finding (not part of this design, but real)

There is **no request-level failover** today: provider health is checked once
at startup (`resolver.py:resolve()` ~258), and the only per-request retry is
same-provider throttle backoff (`service.py` ~857). `fork_call` failures
degrade to empty string (`forked.py:99`). If Anthropic/Bedrock has a
single-region blip mid-task, there's no automatic re-route. This is
independent of the SDK question and could be done on either architecture; the
cleanest insertion is `resolver.get_or_create_provider()` (`resolver.py:354`).

---

## Appendix A — Key file:line index (commit 77497d1)

| What | Location |
|---|---|
| Hybrid branch point | `brain.py:1323-1324` |
| Worker construction (both funnel here) | `brain.py:1372-1392` |
| condense_result (duck-typed contract) | `brain.py:382-393` |
| 4-layer prompt builder (pure fn) | `prompts.py:1320-1436` |
| AgentResult dataclass | `service.py:100-116` |
| Context clearing | `service.py:366`, impl `1818` |
| CachedToolExecutor wrap site | `pr_brain.py:656` |
| CachedToolExecutor.execute | `scratchpad/executor.py:68-165` |
| fork_call + cache prefix | `forked.py:51-167` |
| Provider resolver (startup-only health) | `resolver.py:258-314`, `354-378` |
| Multi-vendor model config | `config/conductor.settings.yaml` (ai_models) |
