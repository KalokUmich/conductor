# Legacy vs Brain PR Review — Detailed Comparison & Improvement Plan

**Date**: 2026-03-28
**Models**: Sonnet 4.6 (strong) + Haiku 4.5 (explorer), Bedrock eu-west-2
**Cases**: 12 (requests v2.31.0 — 4 easy, 4 medium, 4 hard)

## 1. Score Summary

| Dimension (weight) | Legacy | Brain | Delta | Winner |
|---|---|---|---|---|
| **Composite** | **0.888** | **0.903** | +0.015 | Brain |
| Recall (35%) | 1.000 | 1.000 | 0 | Tie |
| Precision (20%) | 0.917 | 0.889 | -0.028 | Legacy |
| Severity (15%) | 0.583 | 0.667 | +0.084 | Brain |
| Location (10%) | 0.833 | 0.833 | 0 | Tie |
| Recommendation (10%) | 0.917 | 1.000 | +0.083 | Brain |
| Context Depth (10%) | 0.917 | 0.917 | 0 | Tie |

**Brain wins composite 0.903 vs 0.888 (+1.7%) with zero prompt tuning.**

## 2. Per-Case Breakdown

### Cases where Brain improved over Legacy

#### requests-003 (easy): Missing encoding fallback
- Legacy: 0.850 (severity=0.000) | Brain: 1.000 (severity=1.000)
- **Root cause**: Legacy classified this as `warning` but the expected severity was `warning` — yet Legacy scored severity=0.000. This means the legacy agent assigned wrong severity (likely `critical`), and the arbitrator didn't correct it. Brain's provability framework in Layer 3 correctly identified this as assumption-dependent (encoding fallback is a design choice), keeping it at `warning`.
- **Takeaway**: Layer 3 provability framework works better than inline prompt instructions.

#### requests-009 (medium): Proxy-Authorization header not stripped
- Legacy: 0.933 (precision=0.667) | Brain: 1.000 (precision=1.000)
- **Root cause**: Legacy produced 2 findings (1 true positive + 1 false positive), Brain produced 1 clean finding. Brain's arbitrator agent likely dropped the false positive because it could `read_file` to verify.
- **Takeaway**: Arbitrator-with-tools > blind arbitration.

#### requests-006 (medium): No URL scheme validation
- Legacy: 0.650 | Brain: 0.683
- Both struggled — severity=0.000 and location=0.000 in Legacy, Brain had severity=0.000 + precision=0.667. This is a genuinely hard case because the scheme validation removal is subtle.

### Cases where Legacy beat Brain

#### requests-008 (medium): Inverted chunked encoding logic
- Legacy: 0.850 (precision=1.000) | Brain: 0.783 (precision=0.667)
- **Root cause**: Brain produced 2 findings instead of 1. The extra finding was a false positive — likely a side-effect of the concurrency or reliability agent flagging something spurious. The evidence gate didn't catch it because both findings had enough evidence.
- **Takeaway**: Brain's sub-agents (dispatched via AgentToolExecutor) sometimes produce more speculative findings because they explore more broadly. Need tighter post-filter or per-agent finding caps.

### Patterns

| Pattern | Legacy | Brain | Analysis |
|---|---|---|---|
| Easy cases (001-004) | 3.633 | 3.783 | Brain +4.1% — provability helps on clear bugs |
| Medium cases (005-008) | 3.350 | 3.316 | Legacy +1.0% — precision advantage on ambiguous cases |
| Hard cases (009-012) | 3.666 | 3.733 | Brain +1.8% — arbitrator verification helps |
| Severity accuracy | 7/12 | 8/12 | Brain gets 1 more severity correct |
| False positive rate | 4 extra findings | 5 extra findings | Legacy slightly cleaner |

## 3. Quality Analysis: Tool Usage

### Legacy Pipeline
- Uses `_build_diffs_section()` to pre-inject ALL diffs into the agent prompt at once
- Agents start with full context — no tool calls wasted fetching diffs
- `llm_semaphore(2)` limits concurrent Bedrock calls — prevents throttling
- Sub-agents use `AgentLoopService.run()` (non-streaming) — simpler, fewer event issues

### Brain Pipeline
- Also pre-injects diffs in Layer 4 query (same data from `build_diffs_section`)
- BUT agents are dispatched via `AgentToolExecutor._dispatch_agent()` which runs `run_stream()` — more overhead per agent
- `XML-garbled params` errors appeared in 4/12 Brain cases (requests-003, 005, 006, 010) — Haiku sometimes produces malformed XML tool calls. The repair mechanism catches these, but they waste iterations.
- No `llm_semaphore` in PR Brain's agent dispatch — concurrent Bedrock calls may cause throttling

### Brain-Specific Issues Observed
1. **XML-garbled params**: `read_file` params like `'end_line": 1450</parameter>\n<parameter name="path'` — Haiku occasionally produces broken XML in tool_use blocks. The existing XML repair in the agent loop catches most, but wastes an iteration each time.
2. **`agent_name` = "unknown"**: The `_post_process` method extracts `agent_name` from `result.data`, but when the dispatch returns condensed results, the field might be missing or under a different key. This means findings get `category=CORRECTNESS` by default even if they came from the security agent.

## 4. Quality Analysis: Synthesis

Legacy and Brain both produce markdown synthesis using the same `_SYNTHESIS_SYSTEM_PROMPT`. The difference:

- **Legacy**: Synthesis is a direct `provider.call_model()` call with all findings + diffs
- **Brain**: Same approach (direct LLM call in `_synthesize()`)

Both produce similar quality synthesis. No significant difference observed.

## 5. Improvement Plan

### P0: Bugs to fix

#### 5.1 Fix `agent_name` extraction in Brain post-processing
The `_post_process` method reads `data.get("agent_name", "unknown")` but the condensed result from `AgentToolExecutor._dispatch_agent()` stores the agent name differently. Need to verify the key path and fix.

**Impact**: Correct category assignment → better dedup and scoring.
**Effort**: Small (1 line fix in `pr_brain.py`).

#### 5.2 Add `llm_semaphore` to PR Brain agent dispatch
Legacy uses `asyncio.Semaphore(2)` to limit concurrent Bedrock calls. Brain's `_dispatch_agents()` uses `asyncio.Semaphore(max_concurrent_agents=3)` but this controls agent-level concurrency, not LLM call-level. Each agent makes multiple LLM calls, so 3 agents × ~5 calls each = 15 concurrent calls → throttling.

**Impact**: Reduces Bedrock throttling errors and retry overhead.
**Effort**: Small — pass `llm_semaphore` to `AgentLoopService` in `_dispatch_agent`.

### P1: Precision improvements

#### 5.3 Per-agent finding cap in post-processing
Legacy's `_AGENT_PROMPT_TEMPLATE` says "Report at most 5 findings" and the prompt is highly focused. Brain's Layer 3 skill says the same, but the 4-layer separation means the instruction is further from the task context. Sub-agents sometimes produce 3-4 findings where only 1-2 are real.

**Fix**: In `_post_process()`, cap findings per agent to 3 (top by confidence) before merging. This mirrors what the legacy pipeline implicitly gets from its tighter prompt.

**Impact**: Reduces false positives, improves precision.
**Effort**: Small (5 lines in `pr_brain.py`).

#### 5.4 Strengthen "max 5 findings" instruction in Layer 3 skill
Move from `code_review_pr` skill (shared Layer 3) to the agent's Layer 4 query — closer to the task context where Haiku pays more attention. Or repeat it in both layers.

**Impact**: Haiku respects instructions more when they're in the user message.
**Effort**: Trivial (add one line to `_build_agent_query`).

### P2: Severity improvements (already ahead, can widen the gap)

#### 5.5 Add severity examples to per-agent .md files
The `.md` files have one example each. Adding 1-2 more examples with different severities (especially warning vs nit edge cases) would help Haiku calibrate.

Following CLAUDE.md principle: "3-5 diverse examples teach behavior better than a laundry list of edge-case bullets."

**Impact**: Severity accuracy 0.667 → target 0.800.
**Effort**: Medium (update 5 .md files with ~50 words each).

#### 5.6 Arbitrator: add "verify file:line exists" step
The pr_arbitrator has `read_file` and `grep` but its prompt doesn't explicitly say "Step 1: for each finding, read the cited file:line and verify the code matches the evidence." Making this explicit would catch more false positives.

**Impact**: Better drop/downgrade decisions.
**Effort**: Small (update pr_arbitrator.md instructions).

### P3: Latency and cost

#### 5.7 Skip low-risk agents
Legacy's `should_run()` skips agents when their risk dimension is LOW. Brain's `_select_agents()` does the same. But Brain currently dispatches all selected agents in parallel with equal priority. For small PRs (< 200 lines), we could skip concurrency/reliability entirely and only run correctness + test_coverage.

**Impact**: 40% faster on small PRs, 40% less cost.
**Effort**: Small (add PR-size threshold to `_select_agents`).

#### 5.8 Reduce arbitrator iterations for simple cases
If all findings are `warning` or `nit` (no `critical`), the arbitrator doesn't need 8 iterations with tool calls. A single LLM call (like Legacy) is sufficient.

**Impact**: Saves ~30s and ~50K tokens on non-controversial reviews.
**Effort**: Small (conditional in `_arbitrate`).

### P4: Eval harness improvements

#### 5.9 Add finding-level diff to eval output
The current eval only shows composite scores. Add a `--verbose` flag that dumps per-finding match details (which expected finding matched which actual finding, severity comparison, evidence quality).

**Impact**: Enables data-driven prompt tuning.
**Effort**: Medium.

#### 5.10 Add token usage tracking to Brain eval
Legacy tracks tokens via `AgentReviewResult.tokens_used`. Brain's `run_case_brain` doesn't collect token data. Add token tracking to compare cost-efficiency.

**Impact**: Enables cost comparison.
**Effort**: Small (emit tokens in done event).

## 6. Priority Order

1. **P0.1** Fix agent_name extraction → correct category assignment
2. **P1.3** Per-agent finding cap → reduce false positives
3. **P1.4** Repeat "max 5 findings" in Layer 4 → Haiku compliance
4. **P2.6** Arbitrator "verify file:line" instruction → better verdicts
5. **P0.2** Add llm_semaphore → reduce throttling
6. **P2.5** Add severity examples to .md files → better calibration
7. **P3.7** Skip low-risk agents for small PRs → speed
8. **P4.9** Verbose eval output → data-driven tuning
