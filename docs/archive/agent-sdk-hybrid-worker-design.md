# Design: SDK Worker — Claude Agent SDK as the worker inner-loop

> Status: **SHIPPED — SDK-only (2026-05-31)** · Originally drafted 2026-05-29 · Author: kalok (+ Claude)
> Baseline commit: `77497d1` (origin/main). File:line refs are against that commit and have since moved.
>
> **What actually shipped (vs this draft):** it landed as **SDK-only, not hybrid.** Steps 02–03
> collapsed providers to **Claude-only** (the Haiku/DeepSeek/Qwen/Nova multi-vendor explorer tier was
> removed), which dissolved the original reason for a hybrid path — so there is **no `is_claude`
> branch**. Every dispatched **leaf** worker runs on the Claude Agent SDK (`SdkWorkerRunner`), while
> **coordinators** (General / Domain / PR Brain) stay in-house on `AgentLoopService`. The real
> discriminator is `brain._dispatch_explore` (`_ORCHESTRATION_TOOLS`), not the single `brain.py:1323`
> line referenced below. Langfuse was retired in favour of task-hierarchy telemetry
> (`TaskTelemetryService` + the `task` table).
>
> Authoritative shipped record: `REFACTOR_EXECUTION_LOG.md` + the live docs (`backend/CLAUDE.md`,
> `docs/GUIDE.md` §6–§8 / §17). **The sections below are the original design rationale, kept for
> history** — where they say "hybrid" / "non-Claude workers" / "open decision", read them as
> superseded by the summary above.

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

## 4bis. SUPERSEDED (2026-05-30): hybrid premise is void → go SDK-only

§4's "forces hybrid" argument rested on a multi-vendor explorer tier. **That tier
no longer exists.** Steps 02–03 (merged) collapsed config to **Claude-only**
(4 Claude models / 2 providers: Bedrock + Anthropic-direct); all non-Claude
models/providers/`OpenAIProvider`/tool-repair were deleted. With no non-Claude
worker, the `is_claude(provider)` branch at `brain.py:1324` is dead, and the
`AgentLoopService` "multi-vendor fallback" has nothing to fall back to.

**Decision (user, 2026-05-30): retire `AgentLoopService` as the worker engine —
ALL dispatched workers run on the Claude Agent SDK.** The coordinator (Brain /
PR Brain / Domain Brain) stays ours (our moat). Hybrid is dropped.

### Feasibility: can the SDK host our whole worker loop? — YES (investigated 2026-05-30)
Two-sided recon (`service.py` mechanisms × SDK capabilities). Disposition of our
10 self-built mechanisms:

| Mechanism (ours) | SDK disposition | Notes |
|---|---|---|
| Core loop (iterate→LLM→tools→repeat) | **SDK NATIVE** (CLI-owned, `max_turns`) | the commodity machinery we wanted to stop maintaining |
| Context compaction (`_clear_old_tool_results`, ~75 LOC, Bedrock-format-coupled) | **SDK NATIVE — delete ours** | CLI auto-compacts; `PreCompact` hook can observe. Biggest win. |
| 4-layer prompt | **reuse as-is** | `system_prompt=<str>` fully replaces the CLI default (verified) |
| Budget controller | **mostly SDK** (`max_budget_usd` + `task_budget`); thin cross-worker-cumulative shim stays ours | |
| **Evidence gate** (our #1 quality moat) | **HOOKABLE — keep logic, re-wire as `PreToolUse`/`Stop` hook** | the one genuinely custom piece that stays; re-validate via eval |
| Throttle/retry | **SDK NATIVE** (CLI retries) | |
| Scatter detection | **HOOKABLE** (`PreToolUse` hook) — keep, lightweight | |
| Thinking-steps (frontend) | **rebuild from message stream** (`ThinkingBlock`/`ToolUseBlock`) | |
| LLM semaphore (concurrency) | **stays ours** (SDK has no worker-pool) — lives at coordinator layer anyway | |
| Tool schemas | **typed `@tool` from `code_tools/schemas.py` `model_json_schema()`** | fixes the spike's generic-schema arg-mis-shaping finding |

Net: SDK absorbs loop + compaction + throttle + most of budget (the catch-up tax).
We keep evidence-gate + scatter (as hooks), cross-worker budget tally, concurrency
gating, the 4-layer prompt, and our tools — i.e. the orchestration/quality moat.

### Cost of going SDK-only (honest)
- **~119 tests** (test_agent_loop 55 + test_brain 64 + integration) mock/instantiate
  `AgentLoopService` → must be rewritten for the SDK path.
- Evidence-gate + scatter move from inline-in-loop to hook callbacks → behavior
  re-validated against the code-review + agent-quality eval bar (§12.2).
- eval harnesses (`eval/agent_quality/run_*.py`) instantiate `AgentLoopService`
  directly → migrate or keep a thin shim.
- ECS image must add Node + `@anthropic-ai/claude-code` (the SDK drives the CLI).

### Step 06 boundary (DECIDED 2026-05-30, user)
**Sub-agents-only first.** Step 06 moves the dispatched sub-agent path
(`brain.py:1372`) onto the SDK; the **coordinator** Brain loop
(`workflow/engine.py:159`) stays on `AgentLoopService` for now. Lower-risk first
move — sub-agent quality can be eval-validated in isolation before touching the
coordinator. Moving the coordinator onto SDK (full `AgentLoopService` retirement)
is a later, separate step once the sub-agent path is proven green.

§5 below is the ORIGINAL hybrid design — kept for history; read it through the
lens of "SDK is now the only worker path, no `is_claude` branch."

---

## 5. Proposed design  (HISTORICAL — hybrid; see §4bis for the SDK-only pivot)

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

**DECIDED (2026-05-29):** Retire Langfuse entirely — switch to SDK-native OTEL.
This also lets us **drop Langfuse's self-hosted database**:
- Delete the `@observe` / `track_generation` wiring in `workflow/observability.py`
  and the `langfuse.*` config flags + secrets (`config.py`, `conductor.*.yaml`).
- Remove the Langfuse DB plumbing: the `langfuse` database creation in
  `docker/init-db.sql`, the `make langfuse-up` target, and any Langfuse compose
  service. (Langfuse managed its own Prisma tables in a separate DB — gone.)
- **Do NOT lose the telemetry** — instead of an external Langfuse DB, **add our
  own tables to the existing Conductor Postgres** (see §5.7.4): per-iteration
  token usage incl. cache tokens, and structured COT/thinking + transcript. OTEL
  export (`CLAUDE_CODE_ENABLE_TELEMETRY=1` + OTLP) covers real-time
  metrics/traces to whatever collector we choose; Postgres covers queryable
  historical audit. Net: one fewer database, one fewer external dependency.

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

## 11. Execution plan — three workstreams + how to run a multi-hour build

> DECIDED (2026-05-29): proceed with Bedrock-only + Claude-only + SDK (D1=go,
> D5=maximal cleanup, D6=retire Langfuse incl. its DB). The three workstreams
> below are the committed work. "All prompts → Claude-adapted" implies the
> explorer tier goes Claude — own that with the team.

### 11.1 Task A — Rewrite all prompts for Claude (high-effort, highest-value)

**Goal:** every prompt (Brain coordinator, agent_factory roles, sub-agent
4-layer, skills) restructured for Claude + the SDK preset model. This is the
"big effort" task and it gates Task B's quality.

Scope & approach:
- Adopt `system_prompt={"preset":"claude_code","append": <our role>}` (§5.6) so
  Anthropic's tuned base carries the generic agent behavior; our files keep only
  **domain identity** (layer ③). Strip from our prompts whatever the preset now
  provides (generic tool-use etiquette, response-format boilerplate).
- Re-audit against CLAUDE.md's own rules: 4-layer separation, 3-4-sentence tool
  descriptions (what/when/when-not/what-it-doesn't-return), examples-over-rules,
  right altitude, positive framing. Many prompts predate the SDK and over-specify.
- Tool descriptions for our MCP `@tool`s matter MORE now (no built-in training
  affinity, R7) — invest here.
- Inventory first: `prompts.py` (~95KB), `config/agents/*.md`,
  `config/agent_factory/*.md`, `config/skills/*.md`, `config/brains/*.yaml`.

**Definition of done:** every prompt file reviewed + rewritten; no prompt
duplicates what the `claude_code` preset provides; eval (Task B) shows no
regression attributable to prompt changes.

### 11.2 Task B — Code review is the flagship: full eval gate, iterate to win

**Hard rule:** code review (PR Brain v2) must pass the **full eval suite** after
the migration. If it doesn't, **keep optimizing until results are clearly good
again** — do not ship a regressed flagship.

Gate:
- Baselines: `eval/code_review/run.py` (12 planted-bug cases) +
  `eval/agent_quality/run_bedrock.py --brain`. Record pre-migration scores as the
  bar to beat (recall 35% / precision 20% / severity 15% / location 10% /
  recommendation 10% / context 10%).
- After each workstream chunk, re-run. Migration is "done" for code review only
  when scores **meet or exceed** the pre-migration baseline, ideally exceed
  (the whole premise is Claude's harness + tuned prompts should help).
- Loop: eval → inspect misses → adjust prompts/dispatch/verifier → re-eval.
  Repeat until clearly better. This is the stop condition, not a fixed iteration
  count.

### 11.3 Task C — Orchestrate the build as a long-running, supervised effort

This is large; it warrants a Claude-driven workflow/goal run. **But "run
autonomously for hours" needs guardrails — here is how to do it safely, not
blindly.**

**Phase ordering (dependency-correct, safe→risky — mirrors §5.7.6 + tasks):**
1. **DB migration** — add Liquibase changesets for the new tables (per-iteration
   token usage incl. cache tokens; structured COT/thinking + transcript).
   Remove Langfuse DB plumbing (`docker/init-db.sql` langfuse DB,
   `make langfuse-up`, compose service). *Reversible; no agent behavior change.*
2. **Config + provider dead-code cleanup** (§5.7.1) — Bedrock+Claude only. Run
   `make test` after; this is mostly deletion with strong test coverage.
3. **Observability swap** — delete `@observe`/`track_generation`; wire OTEL +
   the new Postgres tables.
4. **SDK worker spike** (§7) — prove the 4 seams BEFORE the full build. Gate.
5. **SDK worker build** — `SdkWorkerRunner` behind `brain.py:1323`.
6. **Task A prompt rewrite** — interleave with eval.
7. **Task B eval gate** — iterate to win.

**How to actually run it for hours (mechanics + honesty):**
- Use a **workflow** (deterministic multi-agent orchestration) for the
  fan-out-able parts: e.g. "rewrite N prompt files" (one agent per file, parallel
  + a consistency critic), "trim test files", "audit each provider-coupling
  site." These are ideal — many independent units, each verifiable.
- Use a **`/loop` or scheduled run** for the eval-iterate cycle (Task B): run
  eval → if below bar, spawn a fix agent → re-run, on an interval, until the bar
  is met. The stop condition is the eval score, not a timer.
- **Honesty about autonomy:** the irreversible/structural steps (DB migration,
  deleting provider code, the `brain.py:1323` integration) should be
  **checkpointed, not fully unattended** — each behind its own commit on a branch
  + `make test` gate, with a human (you) reviewing the diff before the next
  structural step. A multi-hour run is safe for: prompt rewrites (reversible
  text), test trimming (tests catch regressions), eval iteration (read-only +
  prompt edits). It is NOT safe to let an agent delete ~6,700 LOC and rewire the
  execution layer with zero checkpoints. Encode that as: workflow does the
  fan-out work + opens commits; structural deletions land behind a test gate; you
  review at phase boundaries.
- **Branch + commit discipline:** one branch per phase, `make test` (1655 tests)
  + `make test-parity` must pass before merging a phase. Code-review eval is the
  extra gate for anything touching PR Brain.
- **Resumability:** a workflow run can pause/resume; structural phases are
  ordered so a failure stops before the risky next step rather than mid-rewire.

**What I (Claude) will do when you say go:** author the actual workflow
script(s) — one per fan-out phase — with per-unit verification and a critic
pass, plus the eval-loop driver for Task B. Each script is reviewable before it
runs. I will NOT kick off a hours-long unattended destructive run without that
checkpoint structure in place.

---

## 12. Branch-discipline execution protocol (how to actually run the refactor)

> DECIDED (2026-05-29): execute the refactor as a sequence of small, independently
> tested steps under a parent branch, with one short-lived child branch per step.
> No step is "done" until its tests pass and it is merged back. This makes the
> checkpoint structure (§11.3) a mechanical rule, not a matter of discipline.

### 12.1 The loop (repeat per step)

```
parent branch: refactor/agent-sdk-migration   (cut once, from main at the
                                                start; accumulates all steps)

for each step S in the plan (§12.3):
  1. git checkout refactor/agent-sdk-migration
  2. git checkout -b refactor/step-NN-<slug>        # child branch off parent
  3. make the change for step S ONLY (keep it small + single-purpose)
  4. write/extend tests for S (see §12.2 — what each kind of step must test)
  5. run the gate:  make test  &&  make test-parity  &&  make lint-check
        - for prompt/Brain/PR steps ALSO run the eval gate (§12.2)
  6. if gate fails → fix on the child branch, re-run, until green
  7. commit on the child branch (one coherent commit, descriptive message)
  8. merge child → parent:
        git checkout refactor/agent-sdk-migration
        git merge --no-ff refactor/step-NN-<slug>
        git branch -d refactor/step-NN-<slug>
  9. go to next step
```

Rules:
- **One step = one child branch = one purpose.** If a step grows, split it.
- **Never merge a red step.** The parent branch is always green.
- `--no-ff` so each step is a visible, revertible merge commit on the parent.
- Parent merges to `main` only at the very end (or at safe milestones), after a
  full `make test` + code-review eval on the parent.

### 12.2 What each kind of step MUST test (the gate)

| Step touches… | Required gate before merge |
|---|---|
| Config / provider deletion | `make test` (1655) + `make test-parity` + `make lint-check` + `make typecheck-strict` |
| **Any prompt** (prompts.py, agents, agent_factory, skills) | the standard gate **PLUS** code-review eval (`eval/code_review/run.py --brain`) **PLUS** agent-quality eval (`eval/agent_quality/run_bedrock.py --brain`) — prompt changes can silently regress quality, so eval is mandatory, not optional |
| **Brain / dispatch / pr_brain** | standard gate **PLUS** the **PR review path** end-to-end **PLUS** **tool functionality** — i.e. run `make test` (covers tool parity + PR Brain tests) and the code-review eval. A Brain change that leaves tools or PR review broken does NOT pass. |
| SDK worker / executor wiring | standard gate **PLUS** the spike-derived checks (§7): Fact Vault hit, WebSocket tool proxy, return-contract mapping |
| Observability / DB tables | `make test` + a Liquibase up/rollback check (`make db-update` / `make db-rollback-one`) |

**Explicit per user:** for any step that changes a **prompt or the Brain**, the
gate must also exercise **PR review and tool functionality** — never merge a
Brain/prompt change on unit tests alone.

### 12.3 Step plan (dependency-ordered; each is one child branch)

Ordered safe→risky so a failure stops before the next, riskier step. Steps 1–3
are reversible/low-risk; 4 is a gate; 5+ are structural.

- **Step 01 — DB tables + Langfuse DB removal.** Add Liquibase changesets for the
  new telemetry tables (per-iteration usage incl. cache tokens; structured COT /
  transcript). Remove the `langfuse` DB from `docker/init-db.sql`, `make
  langfuse-up`, compose service. Gate: DB up/rollback + `make test`.
- **Step 02 — Config collapse to Bedrock+Claude.** Trim `conductor.settings.yaml`
  to 4 Claude models / 2 providers; delete OpenAI/Alibaba/Moonshot secrets
  classes + env vars in `config.py`. Gate: standard.
- **Step 03 — Provider dead-code removal.** Delete `openai_provider.py`,
  tool-repair + schema-sanitization in `claude_bedrock.py`, `enable_thinking`;
  simplify `resolver.py`. Delete `test_bedrock_tool_repair.py`; trim
  `test_ai_provider.py`. Gate: standard.
- **Step 04 — Observability swap.** Delete `@observe`/`track_generation`; wire
  OTEL + write the new Postgres tables from §step-01. Gate: standard + telemetry
  smoke test.
- **Step 05 — SDK worker spike (GATE, may stay on a child branch longer).** Prove
  the 4 seams (§7) on a child branch. Only merge once all four pass. If any
  fails, STOP and revisit the design before structural integration.
- **Step 06 — SDK worker integration.** `SdkWorkerRunner` behind
  `brain.py:1323`; route `@tool` through `CachedToolExecutor`. Gate: standard +
  PR review e2e + tool functionality + code-review eval (Brain change).
- **Step 07..N — Prompt rewrite, one file/group per child branch.** Each prompt
  step gets its own child branch and the FULL prompt gate (§12.2: eval + PR +
  tools). Keep them small so a regression is bisectable to one prompt.
- **Step (final) — Code-review eval gate (Task B).** On the parent branch, run the
  full eval; iterate (more child branches) until scores meet/exceed the
  pre-migration baseline. Only then consider merging parent → main.

### 12.4 Capturing the baseline FIRST (before Step 01)

Before any change, on `main`, record the bar to beat so regressions are
detectable:
```
# on main, pre-refactor
python eval/code_review/run.py --brain            > baseline_code_review.txt
python eval/agent_quality/run_bedrock.py --brain  > baseline_agent_quality.txt
make test                                          # confirm 1655 green starting point
```
Commit these baseline files on the parent branch as the reference. Task B's stop
condition is "meet or exceed these."

### 12.5 Multi-hour / multi-machine notes

- The protocol is **machine-independent**: it's just git + make. Resuming on a new
  computer = `git fetch` + `git checkout refactor/agent-sdk-migration` and
  continue at the next unstarted step. (Push the parent branch so any machine can
  pick it up — see push note at the end of this doc.)
- Steps 01–04 and 07..N are safe to drive with a **workflow / `/loop`** (fan-out
  for prompt files; eval-iterate loop for Task B) BECAUSE the per-step gate +
  merge-only-when-green rule contains the blast radius.
- Steps 05–06 (structural execution-layer rewire) should be **human-reviewed at
  the merge boundary** even if an agent drafts them — review the diff before
  `git merge` into the parent.

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
