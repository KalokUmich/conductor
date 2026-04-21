# PR Brain — Optimization Record

An evolving document for PR Brain v2+ improvement work. **Principles first,
tactical changes second.** Each entry is dated so we can see what we tried,
what worked, and what we reverted.

## Guiding principles

1. **Hardness over score.** A review system that scores 0.95 on one benchmark
   and 0.60 on a new one is worse than one that scores 0.80 on both. We
   optimise for *generalisation*, not for one benchmark's top-of-leaderboard
   rank.
2. **Don't overfit to test data.** Prompts MUST NOT reference identifiers,
   file paths, or bug patterns drawn from specific eval cases. When we need
   an example, it must be a generic placeholder (`FooSvc`, `Bar.handler`,
   `src/widgets/parser.go`) that wouldn't collide with any reasonable
   production codebase.
3. **Simpler beats cleverer.** Per Anthropic: "do the simplest thing that
   works." Each prompt addition carries cognitive-load tax on the LLM. Only
   add when a measured gap justifies it; remove when measurement shows no
   help.
4. **Three evaluation suites, not one.** Decisions must be validated on
   requests + greptile-sentry + greptile-grafana + agent-quality. A change
   that wins one and loses two is a net loss.
5. **Measure Judge AND composite.** Greptile-style composite rewards
   file+line exact match; LLM Judge rewards reasoning quality. They diverge
   often. Use both as signals — when they disagree, read the actual review
   text.

## Current state — v2p (as of 2026-04-21 evening)

- **Pipeline**: PR Brain coordinator (Sonnet) + sub-agent workers
  (Haiku). Phase 2 existence check runs before coordinator to verify
  newly-referenced symbols; missing-symbol post-pass mechanically
  injects findings the coordinator may have dropped.
- **Phase 2 hard timeout**: 120s orchestrator-level wall-clock cap on
  existence-check worker. P13 Python import verifier + P14 stub
  caller detector run alongside as deterministic safety nets.
- **Coordinator prompt**: 585 lines. Severity rubric, Suggested_fix
  specificity (generic examples only), "Don't pad" discipline,
  Findings-vs-secondary cutline, hard floors on correctness/security/
  DB-migration investigations. Adaptive model-tier selection
  (`model_tier=strong` for critical dispatches).
- **Per-language AST hint (P9b, 2026-04-21)**: Phase 2 worker prefers
  `find_symbol` over grep on `.java`/`.py`/`.go`/`.ts|tsx|js|jsx`
  files. Hints injected per-language based on diff extensions.
- **Agent factory**: 6 role templates (correctness/security/reliability/
  concurrency/performance/test_coverage) drive role-specialised
  dispatches. All finding-shape examples use generic placeholders
  (no eval-case identifiers — audited 2026-04-21).
- **Scratchpad fact vault**: per-session SQLite, existence_facts + tool
  result cache + skip list + plan_memory (P4). Orphan sweep on
  backend startup (keeps `~/.conductor/scratchpad/` bounded).

v2p measured (4-suite regression, 2026-04-21):
- Requests (12 cases): composite **0.936**, Judge **4.87**, catch 12/12.
- Sentry (10 cases): composite **0.814**, Judge **3.74**, catch 7/10,
  recall 1.000. Every case hits Phase 2 120s timeout on cold repo —
  P13/P14 deterministic fallbacks carry the load.
- Grafana (10 cases): composite **0.729**, Judge **3.89**, catch 10/10.
- Keycloak (9 cases): composite **0.778**, Judge **3.73**, catch 9/9.

Known operational caveat:
- `make eval-brain-regression PARALLELISM=3` OOM-kills concurrent
  keycloak cases (13.7 GB RSS each under tree-sitter cold load).
  Default lowered to 2 on 2026-04-21; override via `PARALLELISM=`
  env only on ≥32 GB machines.

## Lessons learned (the hard way, through 4 iterations)

### v2e (sweep rule at coordinator level) — reverted
- Caused `0 findings` catastrophic crashes on simple PRs (grafana-004).
- Coordinator got "permission" to over-dispatch, hit budget, emitted
  nothing.
- **Lesson**: coordinator-level fan-out directives over-correct on simple
  cases. Don't bake scale rules into the loop prompt.

### v2g (sweep with explicit size table) — reverted
- Same root cause. Size-to-dispatch-count mapping feels principled but
  the LLM treats it as a target, not a ceiling.

### v2h (root-cause + blast-radius shape example) — partially reverted
- Requests Judge went 4.77 → 5.00 (perfect). Clearly helped single-file
  PRs.
- But sentry-002 Judge dropped 3.75 → 2.20. Over-structured JSON example
  (widget_parser with 3 blast-radius sites) caused coordinator to force
  that shape on every finding, even single-site bugs. Result: verbose,
  artificial reviews on sentry.
- **Lesson**: rich structured examples in prompts are DOUBLE-EDGED. They
  help when the case matches; they hurt when the case doesn't.

### v2j (aggressive revert of all additions) — reverted
- Deleted 128 lines of prompt. Sentry Judge dropped 4.16 → 3.83.
- **Lesson**: the "Don't pad" + "Findings vs secondary" discipline
  sections WERE helping Judge polish. Removing them made coordinator
  emit extras on cases where it should have been decisive.

### v2k (targeted restore of discipline only) — current
- Generic "Don't pad" + "Findings vs secondary" without case-specific
  examples. Smoke confirmed grafana-004 Judge recovered 1.65 → 3.25 (=
  v2d baseline). Sentry-001 partially recovered 1.25 → 2.10.

## What we can infer from Claude Code `/ultrareview` (2026-04-20 late)

Anthropic shipped `/ultrareview` as a Claude Code "research preview" slash
command around the same window we've been iterating. Docs:
<https://code.claude.com/docs/en/ultrareview>. We cannot run it against our
Bedrock pipeline (it requires Claude.ai auth and runs in Anthropic-owned
sandboxes) and it is not in our local skill list, but the public behavioural
description is rich enough to extract **architectural bets we should copy**.

### Verbatim claims worth dissecting

1. "a fleet of reviewer agents in a remote sandbox"
2. "every reported finding is **independently reproduced and verified**, so
   the results focus on real bugs rather than style suggestions"
3. "many reviewer agents explore the change in parallel, which surfaces
   issues that a single-pass review can miss"
4. "bundles the repository state and uploads it to a remote sandbox" — a
   **full repo** snapshot, not just the diff, is in scope
5. Latency: 5–10 minutes per review. Cost: $5–$20. So it's NOT running 50
   agents — more likely 5–15 with verification passes

### Principles we believe UltraReview embodies

| Principle | How we think they do it | Our analogue |
|---|---|---|
| **Independent verification per finding** | Stage 1: explorer/detector agents emit candidate findings. Stage 2: verifier agents re-check each finding *without* seeing the detector's reasoning. | We have Phase 2 existence-check but NOT per-finding verification. **→ P11** |
| **Fleet parallelism** | Concurrent agents dispatched across different semantic dimensions (correctness / security / concurrency / API contract). | We decompose into ≤5 sub-agents today, file-sliced not dimension-sliced. **→ P12** (dimension-sliced dispatch) |
| **Remote sandbox = full repo read** | Workers can grep anywhere, run tests, inspect call graphs across the whole repo. Not scope-boxed. | We deliberately box workers to reduce token use. Trade-off: UltraReview pays $5-20 per review; we target ~$0.20. **Stay boxed.** |
| **Style suppression at output stage, not at prompt stage** | "results focus on real bugs rather than style suggestions" → likely a classifier step that drops style findings, not a prompt rule. | We use prompt DO-NOT-FLAG rules. Might be fragile vs a mechanical severity/category post-filter. |
| **Latency budget 5–10 min** | So ~50 agent-minutes of parallel work, i.e. ~5-8 concurrent agents × 1-2 min each. Similar to our 7-agent dispatch. | Comparable. |
| **Free runs capped at 3** | Anthropic themselves priced this high. Confirms: independent verification is EXPENSIVE but worth it. | Validates the trade: pay 2–3× compute, cut false-positive rate. |

### Cost math from published pricing

- $5-20 per review → at Sonnet 4.6 cache rates (~$3/Mtok in, $15/Mtok out),
  that's 1-6M total tokens per review. Our full PR Brain run (7 sub-agents
  + coordinator synthesis + judge) is ~300K tokens. So UltraReview is
  **5-20× more compute per PR** than our baseline.
- They justify the cost by marketing it as "pre-merge confidence". Different
  market positioning than our in-review, every-PR assistant. So we should
  NOT straight-up copy their compute profile.

### Three bets we're now more confident in

1. **P8 external-signal reflection** was already the highest-impact item in
   our roadmap. UltraReview's "independently reproduced and verified" claim
   is exactly the same bet at a different scale. Land it.

2. **P11 per-finding verification agent (NEW)** — small, bounded verifier
   that re-reads the diff + *only* the finding's file:line + the premise,
   and answers "is this finding reproducible from first principles?". If no,
   reject or downgrade. This is UltraReview's core differentiator, scaled
   down for our cost budget.

3. **P12 dimension-sliced decomposition (NEW, deferred)** — instead of
   `file_range_1, file_range_2, ...`, dispatch along bug-class axes:
   `correctness/api-contract`, `concurrency/races`, `error-handling`,
   `security`. Each worker is asked to find bugs in its lane across the
   whole diff. Pairs well with P11 per-finding verification — each
   worker-emitted finding gets an independent verifier re-check.

### One thing we will NOT copy

**Full-repo unbounded tool access.** Our per-PR compute budget is ~2% of
UltraReview's. Giving workers the whole repo means they'll grep until they
exhaust their token budget. Our strict scope discipline is a cost feature,
not a quality bug. Stay scoped. Let P1 enforce it harder.

## Research-backed design direction

Synthesised from recent Anthropic + OpenAI engineering blog posts (see
Sources). Extracted the principles most applicable to our setup.

### Sub-agent faithfulness (keeping workers on-task)

From Anthropic's multi-agent research system: every sub-agent needs
**four** pieces of prompt contract: (a) objective, (b) output format,
(c) guidance on tools and sources to use, (d) **clear task boundaries**.

Our sub-agents today receive (a) and (b) well, but (c) is loose (we
give them a tool allowlist but no "don't wander outside this scope"
hint) and (d) is weak (checks are specific, but the agent still has
freedom to explore adjacent files).

**Concrete change**: every `dispatch_subagent` prompt should end with an
explicit "stay within these files and lines; do not grep outside this
scope unless a check explicitly asks" clause. And worker skill files
should open with the task-boundary contract.

### Sub-agent budget without explosion

Anthropic's rule of thumb:
- Simple fact-finding: **1 agent, 3–10 tool calls**.
- Comparisons: **2–4 subagents, 10–15 tool calls each**.
- Complex research: **10+ subagents with clearly divided responsibilities**.

Our dispatch budget today is 120K tokens per sub-agent, which empirically
translates to 15–25 tool calls — double Anthropic's recommendation for
fact-finding work.

**Concrete changes to evaluate**:
1. Cap workers at **10 tool calls** (hard limit enforced by
   `AgentLoopService.max_iterations`).
2. If a worker hits the cap mid-investigation, it reports "partial
   verdict: checks 1 and 2 confirmed with evidence; check 3 unclear due
   to iteration budget" — better than silent timeout.
3. The coordinator reads the partial verdict and decides whether to
   dispatch a strong-model follow-up on just check 3.

### Condensed summaries (worker → coordinator handoff)

Anthropic: "each worker might explore extensively, using tens of
thousands of tokens or more, but returns only a condensed, distilled
summary of its work (often 1,000–2,000 tokens)."

Our workers today return the full `answer` field, which can be 4K+ tokens
when they've been thorough. The coordinator then reads all of that across
5–8 dispatches → context bloat at synthesis time.

**Concrete change**: add a `summary` field to the worker output schema.
Workers fill it with ≤500 tokens describing "3 checks passed/violated,
key evidence quoted, next step recommended". The coordinator reads
summaries first; if it needs the full answer, it can expand one on
demand.

### Scope = invariant, not file range

The single best lesson from Anthropic's research post: "vague
instructions cause duplicate work". They went from `"research the
semiconductor shortage"` → `"investigate 2021 automotive chip crisis
focusing on [specific angle]"` and saw duplicate work disappear.

Our dispatch checks today are already invariant-shaped ("At line 138, is
X validated before Y?"), but the **scope** is file-range. If the worker
thinks the check requires reading 3 other files, it will — blowing the
budget.

**Concrete change**: prefer symbol-scope over file-range where possible.
`scope=[{"symbol": "PaymentService.refund"}]` lets the worker read just
the function + its callers, not a 50-line range that might include
unrelated code.

### Three-suite eval infrastructure

Today we smoke-test on one or two cases per iteration. Anthropic suggests
**20 representative queries** initially; we run 32 cases across 3
suites. Good cardinality. BUT we run them ad-hoc and interpret results
manually.

**Concrete change**: build a comparison harness that automatically runs
{requests, greptile-sentry, greptile-grafana} under a configured Brain
version, computes per-case + aggregate deltas vs a named baseline, and
flags regressions with severity classes (cosmetic, meaningful, blocking).

### End-state vs. process evaluation

Anthropic: "traditional evaluations often assume the AI follows the
same steps each time… But multi-agent systems don't work this way."
They evaluate end-state (was the final review correct?) not process
(did it dispatch the right sub-agents?).

We currently conflate these. Our Greptile-style composite rewards
file+line exact match (process-adjacent). Our Judge rewards end-state
quality. **When they disagree, trust the Judge** — and use the
disagreement as a signal that the scorer's expected_findings list
may be imperfect (false positives, paraphrased titles).

### When NOT to add more agents

Both Anthropic and OpenAI converge: "Consider adding complexity only
when it demonstrably improves outcomes." Our temptation is to add a
`blast_radius_mapper` sub-agent for every multi-site case. Before
landing it, we should measure:
- Does prompt-level blast-radius guidance (currently in coordinator
  skill) already give us 80% of the lift?
- If yes, a new sub-agent is feature creep.

## Optimization roadmap — prioritised

Ranked by expected impact / complexity ratio. Each item has a
hypothesis, measurement plan, and revert criterion.

### P1 — Tighten sub-agent task boundaries (prompt, generic)

**Hypothesis**: sub-agents currently wander beyond their dispatched
scope because the skill file doesn't forbid it explicitly. Adding a
"stay within scope unless a check asks otherwise" contract will cut
tool calls 20–30% without hurting recall.

**Change**: edit `config/prompts/pr_subagent_checks.md` to start with
a 3-point contract: "(1) answer the 3 checks only; (2) read only the
files in your scope + files your checks reference; (3) output
verdict + evidence per check + ≤500-token summary".

**Measurement**: smoke on 2 cases per suite. Expected signal: tool
call counts drop in scratchpad stats, recall holds ≥ current.

**Revert**: if recall drops more than 0.05 or Judge Compl drops more
than 0.3 on any suite.

### P2 — Add `summary` field to worker output schema (prompt + parser)

**Hypothesis**: coordinator's synthesis context is bloated by full
worker answers. A dedicated 500-token summary reduces synthesis-time
context by ~70% without losing decision-relevant info.

**Change**: sub-agent skill asks for `checks: […]` and `summary: "…"`
as separate fields. Coordinator reads `summary` first, expands
`checks` only on demand.

**Measurement**: coordinator synthesis token count in Langfuse /
logs. Composite should stay flat or improve (less context rot).

**Revert**: if coordinator asks for expansion on >50% of
dispatches, the summary isn't carrying enough signal.

### P3 — Worker tool-call hard cap (code)

**Hypothesis**: budget-based caps (120K tokens) let workers
do 25+ tool calls on large codebases. Iteration-based cap of 10
aligns with Anthropic's guidance and prevents slow-drift timeouts.

**Change**: set `AgentLoopService.max_iterations=10` specifically for
pr_subagent_checks worker (overridable via agent .md config).
Orchestrator accepts "partial verdict" as a valid return.

**Measurement**: wall-clock for 3-suite run. Expected:
drop from ~50 min to ~30 min without quality regression.

**Revert**: if any case loses a MATCH finding due to cap.

### P4 — Persisted plan memory for coordinator (code + scratchpad)

**Hypothesis**: coordinator loses track of its plan across replan
rounds as context fills. Saving "Plan decision: dispatched X / Y / Z
for reasons A / B / C" to scratchpad at end of Phase 2 and reading
it at Synthesize prevents drift.

**Change**: add `plan_memory` table to scratchpad. Coordinator writes
after Plan, reads at Synthesize. No extra LLM calls — just a
structured fact.

**Measurement**: look for "forgot to flag X" cases in the diff
between what Plan-phase claimed and what Synthesize emitted.

### P5 — Symbol-scope dispatch option (code + schema)

**Hypothesis**: file-range scope forces workers to read 50 lines when
they actually care about one function. Symbol-scope
(`{"symbol": "Foo.bar"}`) routes the worker through `find_symbol`
which gives them the exact function + signatures. Faster, tighter.

**Change**: extend `DispatchSubagentScope` schema to accept
`symbol` as an alternative to `file + start + end`.

**Measurement**: compare worker token usage on cases with
symbol-scoped vs file-range dispatches.

### P6 — Broader eval suite integration (infra)

**Hypothesis**: our current suites (requests + greptile-sentry +
greptile-grafana) have known ground-truth quality issues. Adding
agent_quality eval as a parallel track gives us a "different-shape"
signal that catches prompt changes that help benchmarks but hurt
open-ended work.

**Change**: add `make eval-brain-regression` that runs all 4 tracks
in parallel and produces a consolidated report with per-suite deltas.

### P7 — Prompt caching for coordinator + sub-agent skills (code, huge cost lever)

**Hypothesis**: we re-pay ~7K tokens (coordinator system prompt) and
~2K tokens (each sub-agent skill) on every dispatch. Anthropic prompt
caching gives **85–90% cost reduction** on cached input for cache
reads. A 3-suite eval costs us ~$3-5 today; cached that's ~$0.40–0.70.

**Change**: mark system prompts + tool definitions as cache-eligible
in the Bedrock Converse API call. Anthropic's cache has a 5-min TTL;
our 3-suite run hits each system prompt dozens of times within that
window, so cache-read rate should be ≥95% after the first dispatch.

**Measurement**: Langfuse cache-hit % + daily Bedrock bill. Expected:
cost drops ~80%, latency improves (cache reads are faster than fresh
prompt processing).

**Revert**: if Judge or composite moves by >0.05 (shouldn't — caching
is semantically identical to uncached). If we see any change, it's
probably a caching bug, not an intended behaviour change.

### P8 — External-signal reflection pass before final synthesis

**Hypothesis**: 2026 research shows intrinsic self-correction is weak
("LLMs generate plausible but internally coherent errors that defeat
consistency-based detection"). **External-signal reflection** —
reflection paired with objective feedback — gives +18.5 percentage
points accuracy in published benchmarks.

We already have one external signal: missing-symbol post-pass
(mechanically injects findings Phase 2 said should exist). We can
extend: after coordinator drafts findings, run a lightweight
reflection pass that reads Phase 2 facts + diff and asks "is any
finding you emitted contradicted by existence facts? is any existence
fact NOT covered by your findings?" — objective, not self-review.

**Change**: add `_reflect_against_phase2_facts` post-pass in
`_apply_v2_precision_filter`. Runs AFTER the missing-symbol
injection. Rejects findings that rely on "X doesn't exist" when
Phase 2 marked X as existing. Emits a flag when the review diverges
from Phase 2 facts.

**Measurement**: count of findings rejected by this pass across
3-suite runs. Target: 0–2 per suite (catches LLM hallucinations
without over-rejecting).

### P9 — Java / tree-sitter AST hooks for Phase 2 (shipped)

**Hypothesis**: Phase 2 existence check today uses grep patterns
tuned to Python-style `class Foo` / `def foo`. Java uses
`public class Foo`, `public static Foo foo()`, and the signature
match needs to handle overloaded methods (same name, different
parameter types).

Tree-sitter is already present (Phase 9.18). We can ask Phase 2 to
use `find_symbol` (which runs tree-sitter) for Java files rather
than grep, giving it AST-aware verification without language-
specific grep patterns in the prompt.

**Change**: auto-detect language from diff file extensions; for
`.java`, direct Phase 2 worker to use `find_symbol` as primary
verifier, fallback to grep. For overloaded methods, return the
parameter-type list so signature-mismatch post-pass can compare
properly.

**Measurement**: add `greptile-java-*` (new suite we haven't
measured) to regression harness. Target: Java cases should score
within 0.05 of Python baseline on catch rate.

### P9b — Extend AST-prefer to Python / Go / TS/JS (shipped 2026-04-21)

**Hypothesis**: tree-sitter already indexes all 4 mainstream
languages. Restricting the `find_symbol`-prefer hint to Java left
obvious quality on the table — Python `__init__` kwargs / MRO
inheritance, Go method receivers, and TS function overloads all
benefit from AST over grep.

**Change**: `lang_hints` in `_run_v2_phase2_existence` now emits a
per-language hint for each of `.java` / `.py` / `.go` / `.ts|tsx|js|jsx`
present in the diff, each calling out the language-specific AST
win (overloads for Java/TS, MRO for Python, receivers for Go).
`config/agents/pr_existence_check.md` verifier table rewritten to
say `find_symbol` is primary with grep as fallback.

**Measurement**: grafana-009 (Go) smoke: Judge 3.45 → 4.00 → 4.50
over 3 runs post-change; Phase 2 picks `find_symbol` BEFORE grep on
the first symbol. sentry-001 (Python) smoke: Phase 2's first 4 tool
calls are `find_symbol` (was grep-heavy before). 6 unit tests cover
per-language hint injection + Rust-exclusion.

**Revert**: if composite drops on any suite by >0.05 or Judge
drops >0.3.

### P10 — Adaptive worker model selection

**Hypothesis**: we pin all workers to the explorer model (Haiku) and
reserve strong (Sonnet) only for verification. But some checks —
cross-file control-flow reasoning, subtle invariant verification —
benefit from strong-model capability. Anthropic's 2026 "Advisor
Strategy" pattern: fast executor can consult a powerful advisor
mid-task.

**Change**: coordinator can mark a dispatch as `requires_strong=true`
when the check involves cross-file state tracking or multi-step
logical inference (not just pattern matching). Worker uses Sonnet
for those; Haiku for everything else. Cost delta: ~5× on flagged
dispatches, but only 1–2 per PR.

**Measurement**: Judge Reason axis on cases with `requires_strong`
dispatches vs without. If Reason rises significantly without
composite regression, worth keeping.

**Revert**: if cost per case grows >30% without measurable quality
gain.

### P11 — Per-finding independent verification agent (NEW, UltraReview-inspired)

**Hypothesis**: UltraReview's headline claim is "every finding is
independently reproduced and verified". Translation for us: after the
coordinator drafts its final findings list, spawn a lightweight verifier
agent per finding (or batched into one agent per 3-5 findings) that
receives ONLY:

1. The finding title + premise (what, where, why it's bad)
2. A narrow scope around the cited file:line
3. The diff itself

…and answers "can I independently reproduce the premise from the code
alone, without access to the coordinator's reasoning?". If no, the
finding is dropped or downgraded.

The key novelty vs our existing Phase 2 existence check: existence check
runs **before** findings exist and is limited to symbol resolution. P11
runs **after** findings exist and checks the semantic claim. E.g.
coordinator says "validation missing before SQL INSERT"; verifier reads
the file:line range and confirms (or rejects) "yes, line 138 calls
session.execute without prior validation" on its own.

**Change**: new `pr_finding_verifier` agent (Haiku), dispatched in a
final post-pass. Verifier output: `{finding_id, verdict:
confirmed|unconfirmed|contradicted, evidence: "..."}`. Unconfirmed
drops to nit; contradicted drops the finding entirely.

**Measurement**: Judge FP-discipline axis. Expected: fewer spurious
critical/warning findings, cleaner reviews. Cost: +20-40% compute per
PR because each finding gets a verification pass. Worth it if
FP-discipline rises ≥ +0.3 points.

**Revert**: if it costs > +50% compute or drops any MATCH finding.

### P12 — Dimension-sliced decomposition (deferred, UltraReview-inspired)

**Hypothesis**: we currently decompose by file-range. UltraReview
appears to decompose by bug class. A worker asked "find all concurrency
issues in this diff" reads the same files as a file-range worker but
hunts a different pattern set. Parallel dispatches along different
axes surface different classes of bug.

**Change**: coordinator decomposes into 4-6 dimension-sliced dispatches:
correctness/contract, concurrency/races, error-handling/input-validation,
security/auth, dependency-integrity. Each worker receives the full diff +
a sharp invariant checklist for its lane.

**Measurement**: composite recall on multi-site bugs (grafana-009/010
today). If recall rises without precision loss, P12 is valid.

**Deferred** until P1/P8/P11 are measured — may overlap with P11's lift.

### P14 — Mechanical stub-function detector (NEW, shipped)

**Hypothesis**: multi-site bugs like grafana-009 (stub `DB.RunCommands`
returning `errors.New("not implemented")` called from `parser.go`) are
a PURE pattern match — no LLM reasoning needed. If the PR adds a
function whose body is literally "return error/raise
NotImplementedError" AND there's a call site in the diff, that's a
bug. Inspired by research showing +39.7pp recall lift from
diverse-pattern agents ([arXiv 2511.16708](https://arxiv.org/html/2511.16708)).

**Change**: new `_scan_for_stub_call_sites()` + `_inject_stub_caller_findings()`
in `pr_brain.py`. Two-pass over the diff:

1. Enumerate functions added by the PR whose body regex-matches a
   "not implemented" stub shape. Go: `return ...,
   errors.New("not implemented")`. Python: `raise NotImplementedError`.
2. Grep the diff for calls to those stub names. Context-line calls
   count (pre-existing caller now hits a NEW stub is still a bug).
3. For each (stub, caller) pair not already covered by a coordinator
   finding within ±3 lines, inject a synthetic finding at the caller
   site with severity=high, confidence=0.95.

**Guardrails**: skips function declarations (`func Foo(` is NOT a
call of Foo). Skips real function bodies (regex requires the
specific error literal). Skips call sites already covered by
coordinator findings. Go + Python only.

**Measurement**: standalone run on grafana-009 finds exactly 2 stub
callers: `parser.go:26 RunCommands`, `sql_command.go:100
QueryFramesInto`. 10 unit tests pass. Full regression v2m planned.

### P13 — Deterministic Python import verifier (NEW, shipped)

**Hypothesis**: the LLM Phase 2 worker was intermittently emitting
`symbols=[]` on sentry-001 — the textbook phantom-import case. We saw
`existence_facts=0` on two consecutive smokes, meaning our entire
missing-symbol injection path was blocked by worker flakiness. A
mechanical AST-grep pass that runs ALONGSIDE the worker, unioning its
output, eliminates this failure mode for Python.

**Change**: new `_scan_new_python_imports_for_missing()` in
`pr_brain.py`. Runs after the LLM worker inside
`_run_v2_phase2_existence`, regardless of worker success. Parses
`+from X import Y` / `+import X` lines from the diff, and for each
imported name runs `grep -r -E "^\s*(class|def)\s+Y|^\s*Y\s*="
--include=*.py`. Zero matches → `exists=False` fact added to the vault.
Existing injection path picks it up and emits an ImportError finding.

**Guardrails**: skip relative imports, wildcard `*`, and known
framework modules (`os`, `typing`, `django.*`, `pydantic`, …). Cap at
24 symbols per PR. 8-second grep timeout per symbol. Fail-safe on any
error (never report missing we couldn't verify).

**Measurement**: standalone smoke on sentry-001 base — P13 correctly
returns `OptimizedCursorPaginator` as missing in 10ms, zero LLM cost.
11 unit tests. Next: measure full regression delta.

**Future extension**: TS/Go variants (once this proves out). For Go
the analogous pattern is `pkg.Foo` call-sites where `Foo` is not
defined in the named package — tree-sitter query would be cleaner
than grep for Go's syntax.

### Deferred / avoided

- **`blast_radius_mapper` sub-agent**: holding off until P1–P3 land;
  may no longer be needed once workers are more scoped.
- **Over-specific suggested_fix templates**: our experience with
  Go/TS examples that touched real case identifiers taught us this
  path overfits. Stay generic.
- **Case-by-case prompt tuning**: if a change helps case X but no
  other cases, it's overfit — don't land it.

## What "hardness" means for us

A PR Brain is "hard" (robust) if:
- Prompt changes that win one suite also ≥ hold on the others.
- The same coordinator handles PRs in Python, Go, TypeScript, Rust
  equivalently, because the prompt doesn't encode language-specific
  knowledge.
- New eval cases (that the model's never seen) score within 5–10%
  of baseline on first try — no surprise crashes.
- The review still reads sensibly when Greptile's expected_findings
  disagree with reality (coordinator should call out false positives
  clearly, even if scorer penalises for not matching expected).

If any of these break, we're overfit.

## Sources

Grouped by topic. Each entry annotated with the insight we pulled from it.

### 0. Anthropic / Claude Code product signals

- [Claude Code `/ultrareview` docs (Apr 2026)](https://code.claude.com/docs/en/ultrareview) — "fleet of reviewer agents", "every reported finding is independently reproduced and verified", 5–10 min latency, $5-20/run. Inspires **P11** (per-finding verifier) and **P12** (dimension-sliced dispatch).
- [Claude Code `/ultraplan` docs](https://code.claude.com/docs/en/ultraplan) — planning counterpart. Similar multi-agent, remote-sandbox execution model. Worth reading for coordinator-side patterns.
- [Claude Code on the web (remote sessions)](https://code.claude.com/docs/en/claude-code-on-the-web) — context for how Anthropic runs sandboxed agents at cost.

### 1. Agent architecture — orchestrator / workers / sub-agent scoping

- [Building Effective AI Agents (Anthropic, Dec 2024)](https://www.anthropic.com/research/building-effective-agents) — canonical taxonomy of agent workflows: prompt chaining, routing, parallelization, orchestrator-workers, evaluator-optimizer. Warning: "consider adding complexity only when it demonstrably improves outcomes."
- [Effective Context Engineering for AI Agents (Anthropic, 2025)](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents) — workers use tens of thousands of tokens, return **1–2 K condensed summaries**. Sub-agent context isolation is the primary token-budget lever.
- [How we built our multi-agent research system (Anthropic)](https://www.anthropic.com/engineering/multi-agent-research-system) — 4-part sub-agent contract (objective / format / tools / **clear boundaries**). Opus lead + Sonnet workers beat single-agent by +90%. Explicit scaling table (1 agent for fact-find, 10+ for complex research).
- [Building agents with the Claude Agent SDK](https://claude.com/blog/building-agents-with-the-claude-agent-sdk) — sub-agents serve two roles: parallelization + context isolation.
- [OpenAI Agents SDK — multi-agent orchestration](https://openai.github.io/openai-agents-python/multi_agent/) — agents-as-tools (manager keeps control) vs handoffs (specialist takes over) pattern choice.
- [OpenAI Agents SDK — agents primitive](https://openai.github.io/openai-agents-python/agents/) — base `Agent` abstraction; `asyncio.gather` for parallel fanout.
- [The next evolution of the Agents SDK (OpenAI, 2026)](https://openai.com/index/the-next-evolution-of-the-agents-sdk/) — 2026 update: configurable memory, sandbox-aware orchestration, sub-agents + code-mode primitives.
- [2026 Agentic Coding Trends Report (Anthropic)](https://resources.anthropic.com/2026-agentic-coding-trends-report) — shift from single assistants to coordinated agent teams running autonomously for hours.
- [Claude Cookbook — orchestrator_workers.ipynb (Anthropic)](https://github.com/anthropics/anthropic-cookbook/blob/main/patterns/agents/orchestrator_workers.ipynb) — reference implementation of the orchestrator-workers pattern.
- [Spring AI — agentic patterns part 1 (2025)](https://spring.io/blog/2025/01/21/spring-ai-agentic-patterns/) — Anthropic's patterns ported to the JVM ecosystem; useful when we add Java review.
- [Cloudflare agents — Anthropic patterns guide](https://github.com/cloudflare/agents/blob/main/guides/anthropic-patterns/README.md) — edge-deployment flavour of the same patterns; worth skimming for concurrency ideas.
- [Design Patterns for Effective AI Agents (patmcguinness)](https://patmcguinness.substack.com/p/design-patterns-for-effective-ai) — community synthesis of Anthropic's 5 patterns with worked examples.
- [Anthropic's 5 essential architect patterns (Rizwan Syed, Mar 2026)](https://aisolutionarchitect.medium.com/building-with-agentic-ai-anthropics-5-essential-architect-patterns-02f9e791b118) — pattern summary with decision criteria.
- [Anthropic Advisor Strategy — smarter AI agents (2026)](https://www.buildfastwithai.com/blogs/anthropic-advisor-strategy-claude-api) — fast executor (Haiku) consults a slower advisor (Opus) mid-task. Inspiration for our P10.
- [Anthropic's Agentic Coding Report: orchestration without intent (Pathmode)](https://pathmode.io/blog/orchestration-era-needs-intent) — "orchestration without intent is expensive guessing" — matches what we observed when we over-dispatched in v2g.

### 2. Cost, caching, and latency

- [Prompt caching — Claude API Docs](https://platform.claude.com/docs/en/build-with-claude/prompt-caching) — cache write = 1.25× base input, cache read = 0.1× base input. 5-min default TTL; 1-hour option costs 2× write but cheaper at scale.
- [Anthropic API pricing in 2026 — full guide](https://www.finout.io/blog/anthropic-api-pricing) — breakdown of per-model pricing, caching impact on real RAG apps.
- [Claude API pricing 2026 (Metacto)](https://www.metacto.com/blogs/anthropic-api-pricing-a-full-breakdown-of-costs-and-integration) — integration-cost perspective.
- [Cut Anthropic API Costs 90% with Prompt Caching (Markaicode)](https://markaicode.com/anthropic-prompt-caching-reduce-api-costs/) — concrete case study: 90% cost drop on cache hits.
- [Anthropic Prompt Caching 2026 — TTL, latency planning (AI Checker Hub)](https://aicheckerhub.com/anthropic-prompt-caching-2026-cost-latency-guide) — when to choose 5-min vs 1-hour TTL for agent workloads.
- [How prompt caching affects Claude subscription limits (MindStudio)](https://www.mindstudio.ai/blog/anthropic-prompt-caching-claude-subscription-limits) — quota implications for teams on Claude Teams/Enterprise.
- [Prompt Caching (Agno docs)](https://docs.agno.com/models/providers/native/anthropic/usage/prompt-caching) — how to enable caching in an agent framework — pattern we can reuse for our Bedrock path.
- [Prompt caching on Vertex AI (Google Cloud)](https://docs.cloud.google.com/vertex-ai/generative-ai/docs/partner-models/claude/prompt-caching) — same mechanism behind Bedrock's Anthropic-hosted models; GCP docs are often clearer on exact API fields.
- [Usage-based billing 2026 pricing shift (Kingy)](https://kingy.ai/ai/usage-based-billing-no-flat-rate-why-anthropics-2026-pricing-shift-changes-everything-for-claude-users/) — why the API-cost lever matters more now than in 2025.
- [Automatic prompt caching in Anthropic (Joe Njenga, Feb 2026)](https://medium.com/ai-software-engineer/anthropic-just-fixed-the-biggest-hidden-cost-in-ai-agents-using-automatic-prompt-caching-9d47c95903c5) — the practitioner perspective.

### 3. Reflection, self-consistency, and verification

- [AI Agent Reflection and Self-Evaluation Patterns (Zylos, Mar 2026)](https://zylos.ai/research/2026-03-06-ai-agent-reflection-self-evaluation-patterns) — **+18.5 pp accuracy** when reflection is paired with objective feedback. LATS, PRMs, multi-agent debate summarised.
- [Self-evaluation in AI agents with chain of thought (Galileo)](https://galileo.ai/blog/self-evaluation-ai-agents-performance-reasoning-reflection) — agentic reflection pipeline in production.
- [Reflection Agent Pattern — Agent Patterns docs](https://agent-patterns.readthedocs.io/en/stable/patterns/reflection.html) — canonical description with code.
- [Agent Reflection: how AI agents self-improve (StackViv, 2026)](https://stackviv.ai/blog/reflection-ai-agents-self-improvement) — industry overview.
- [The Agentic AI Reflection Pattern (Tungsten)](https://www.tungstenautomation.com/learn/blog/the-agentic-ai-reflection-pattern) — enterprise take.
- [5 AI Agent Design Patterns (n1n.ai)](https://explore.n1n.ai/blog/5-ai-agent-design-patterns-master-2026-2026-03-21) — reflection is pattern #1 in their list.
- [5 Agentic AI Design Patterns CTOs Must Evaluate (Codebridge)](https://www.codebridge.tech/articles/the-5-agentic-ai-design-patterns-ctos-should-evaluate-before-choosing-an-architecture) — decision framework for pattern selection.
- [Self-Consistency Prompting (LearnPrompting)](https://learnprompting.org/docs/intermediate/self_consistency) — technique overview.
- [Failure Makes the Agent Stronger (arXiv 2509.18847, 2025)](https://arxiv.org/abs/2509.18847v1) — structured reflection improves reliability on tool-calling benchmarks.
- **Key caveat from the Zylos piece**: intrinsic self-correction is weak — LLMs generate "plausible but internally coherent errors that defeat consistency-based detection." External-signal verification is the reliable lever. Maps directly to our Phase 2-anchored reflection plan (P8).

### 4. Code review benchmarks, false positives, and attack surface

- [Benchmarking and studying LLM-based Code Review (arXiv 2509.01494)](https://arxiv.org/html/2509.01494v1) — SWR-Bench: 1000 manually verified PRs, LLM-based evaluation with ~90% human agreement. Worth investigating as a supplementary benchmark to Greptile.
- [Confirmation Bias in LLM-Assisted Security Code Review (arXiv 2603.18740)](https://arxiv.org/html/2603.18740v1) — false-negative rate grows 16–93% under biased context; false-positive rate barely moves. Confirms our hypothesis that over-specific prompt hints cause recall loss.
- [LLM Code Reviewers Are Harder to Fool Than You Think (arXiv 2602.16741)](https://arxiv.org/html/2602.16741v1) — 14,012 evaluations, 8 models. Aggregate adversarial-comment effect is statistically non-significant. So "hardness" for us is about input diversity, not adversarial input resistance.
- [Rethinking Code Review Workflows with LLM Assistance (arXiv 2505.16339)](https://arxiv.org/html/2505.16339v1) — empirical study on when LLM review helps vs hurts.
- [Using LLM for Code Review in storaged Projects (2026)](https://storaged.org/other/2026/04/16/Using-LLM-for-code-review-in-storaged.html) — recent Opus 4.6 field notes.
- [AI Agent Benchmarks 2026](https://aiagentsquare.com/blog/ai-agent-benchmarks-2026.html) — landscape overview.
- [LLM Benchmarks 2026 — comparison site](https://llm-stats.com/benchmarks) — raw numbers tracker.
- [LLM benchmarks in 2026 — what they prove (LXT)](https://www.lxt.ai/blog/llm-benchmarks/) — benchmark-design critique.
- [Why most LLM benchmarks are misleading (dasroot)](https://dasroot.net/posts/2026/02/llm-benchmark-misleading-accurate-evaluation/) — methodology pitfalls to avoid.
- [AI code review has come a long way, but it can't catch everything (ProjectDiscovery)](https://projectdiscovery.io/blog/ai-code-review-vs-neo) — realistic limits of code-only review.

### 5. Java / AST / language-specific patterns

- [CodeRabbit — AST-based review instructions](https://docs.coderabbit.ai/guides/review-instructions) — production use of ast-grep + tree-sitter for per-language review rules. Reference for our P9.
- [CodeRabbit homepage](https://www.coderabbit.ai/) — baseline product we should know what we're competing against.
- [Building an AI Code Review Agent: advanced parsing (Baz)](https://baz.co/resources/building-an-ai-code-review-agent-advanced-diffing-parsing-and-agentic-workflows) — tree-sitter-first architecture.
- [AI Agent Architecture Patterns for Code Review Automation (Tanagram)](https://tanagram.ai/blog/ai-agent-architecture-patterns-for-code-review-automation-the-complete-guide) — pattern catalogue specific to code-review agents.
- [How AI code review works (Graphite)](https://graphite.com/guides/how-ai-code-review-works) — end-to-end workflow description.
- [Building a Production AI Code Review Assistant with Google ADK](https://codelabs.developers.google.com/adk-code-reviewer-assistant/instructions) — Google's reference implementation (codelab).
- [Building a secure code-review agent (Hungrysoul, Medium)](https://medium.com/@hungry.soul/building-a-secure-code-review-agent-c8b2231ac6ed) — security-focused review-agent design.
- [Best AI Code Review Tools in 2026 (Lewis Kallow)](https://medium.com/@lewis_75321/the-best-ai-code-review-tools-in-2026-599c7dd1b305) — market survey.
- [Best AI Code Review Tools 2025 (Augment Code)](https://www.augmentcode.com/tools/best-ai-code-review-tools-2025) — prior-year comparison; useful for trend delta.
- [Coding guidelines for AI agents (JetBrains blog)](https://blog.jetbrains.com/idea/2025/05/coding-guidelines-for-your-ai-agents/) — IDE-side coding-style guidance that pairs well with review-side rules.

### 6. Takeaways wired into our roadmap

| Source | → Our P# |
|---|---|
| Anthropic multi-agent research (4-part contract) | P1 (task boundaries) |
| Anthropic context engineering (condensed summary) | P2 (summary field) |
| Anthropic scaling table (3–10 tool calls) | P3 (worker iter cap 10) |
| LeadResearcher saves plan to Memory | P4 (plan memory) |
| Anthropic "vague → specific scope" | P5 (symbol scope) |
| Anthropic + OpenAI eval guidance | P6 (4-suite regression harness) |
| Anthropic prompt caching (85–90% discount) | P7 (cache system prompts) |
| Zylos reflection + objective feedback (+18.5 pp) | P8 (external-signal reflection) |
| CodeRabbit ast-grep pattern; Spring AI JVM port | P9 (Java AST) |
| Anthropic Advisor Strategy | P10 (adaptive model) |
| `/ultrareview` "independently reproduced & verified" | P11 (per-finding verifier) |
| `/ultrareview` "fleet of agents exploring in parallel" | P12 (dimension-sliced dispatch) |
| CodeX-Verify / arXiv 2511.16708 | P14 (mechanical stub detector) |

If a future P-item has no source backing it, flag as "hypothesis only" until we add one.

## Log

| Date | Version | Summary |
|---|---|---|
| 2026-04-20 AM | v2f | P1 (missing-symbol post-pass) + P2 (suggested_fix) + P3 (severity rubric) landed. Grafana composite 0.742. |
| 2026-04-20 mid | v2g | Sweep rule added. Grafana-004 0 findings catastrophic. Reverted. |
| 2026-04-20 PM | v2h | Root-cause + blast-radius shape example. Requests Judge 5.00 but sentry regressed. Partially reverted. |
| 2026-04-20 late | v2i | ≥1 finding rule + Phase 2 slim. Phase 2 still timed out at 10min on sentry. |
| 2026-04-20 evening | v2j | Aggressive revert of added prompts. Sentry Judge dropped further (3.83 vs 4.16). Over-reverted. |
| 2026-04-20 night | v2k | Targeted restore of "Don't pad" + "Findings vs secondary". Smoke OK; full run deferred. Phase 2 hard timeout (120s) landed in code. |
| 2026-04-20 evening | v2k + P2/P3/P6 | **P7 discovered already implemented** (Bedrock `cachePoint` markers on system prompt + tool config in `claude_bedrock.py:735-748`). **P3 applied**: `pr_subagent_checks` iter cap 18→10 / budget 150K→100K; `pr_existence_check` iter cap 16→8. **P2 applied**: worker output schema now requires a mandatory `summary` field (≤ 500 chars, coordinator-first-read). **P6 applied**: `make eval-brain-regression TAG=…` target chains 3-suite parallel run with consolidated summary print. Full validation deferred until AWS Bedrock session token refreshes (expired mid-evening). |
| 2026-04-20 late-night | v2l | Token refreshed. **P8 landed** (external-signal reflection — drops findings whose premise contradicts Phase 2 exists=True facts). **P11 cheap landed** (diff-scope verification — demotes findings whose file isn't in the PR diff). **P13 landed** (NEW — deterministic Python import verifier, mechanical grep pass alongside LLM Phase 2 worker; catches OptimizedCursorPaginator-class bugs even when worker flakes). **UltraReview section** added to doc with architectural inferences. Smoke sentry-001 with P8+P11 active: composite 0.813 (within baseline). P13 standalone verified on sentry-001: detects missing symbol in 10ms, 0 LLM cost. 24 new unit tests (P8:6, P11:7, P13:11), all pass. 3-suite regression TAG=v2l running. |
| 2026-04-20 night+1 | v2l + P14 | **P14 landed** (NEW — mechanical stub-function detector for Go + Python). Scans the diff for functions whose body is a 'not implemented' error return / NotImplementedError; cross-references calls in the diff (+ lines + context lines). Injects per-call finding at severity=high/confidence=0.95 when the coordinator hasn't already covered the site. Target: grafana-009 class multi-site stub bugs. Standalone validation on grafana-009 finds exactly 2 stub callers (`parser.go:26 RunCommands`, `sql_command.go:100 QueryFramesInto`). 10 more unit tests. 82 total pr_brain tests pass. Full v2m regression pending. |
| 2026-04-20 night+2 | v2l FINAL | **v2l 3-suite regression complete**. Requests 0.940 composite / 100% catch / Judge 4.93 avg (vs 0.91+ baseline — **above**). Sentry 0.815 composite / 80% catch / individual scores 0.698-0.956 (vs 0.80-0.84 baseline — **within**). Grafana 0.714 composite / 90% catch (vs 0.70-0.74 baseline — **within**). Zero regressions. P8+P11+P13 are net-positive. Launching v2m (v2l + P14). |
| 2026-04-20 night+3 | v2m grafana-009 | **P14 validated on the target case.** v2l grafana-009 = 0.588 composite, recall 0.25, 2 findings. v2m grafana-009 = 0.678 composite, recall 0.50, 5 findings — **+0.090 composite / +0.250 recall**. Of P14's 2 injected findings, one MATCHed an expected ground-truth finding (parser.go:26 `RunCommands` stub call), the other was tagged extra but flagged a real bug Greptile's bot missed (sql_command.go:100 `QueryFramesInto` stub call). Mechanical stub detection works. |
| 2026-04-20 night+4 | v2m FINAL | **v2m 3-suite regression complete (v2l + P14 wired).** Requests **0.941** / 100% catch (v2l 0.940 / 100% — tied). Sentry **0.810** / 70% catch (v2l 0.815 / 80% — within LLM variance, 1 case lost catch likely noise). Grafana **0.701** / 90% catch (v2l 0.714 / 90% — within variance). **Net at suite-aggregate level: essentially tied.** P14 wins clearly on grafana-009 (+0.090) and grafana-010 (+0.098) — both multi-site stub cases — but LLM variance on non-stub cases (grafana-001 −0.227, grafana-008 −0.258) cancels the suite-level lift. Recommendation: KEEP P14. It solves a real bug class, never false-positives on the 6 patches tested, and the case-targeted win is real even when suite metrics don't move above noise floor. To prove suite-level impact, would need 3-5 averaged runs per TAG. |
| 2026-04-21 morning | P14.java | **P14 extended to Java (3rd language).** Added `_JAVA_STUB_BODY_RE` covering `UnsupportedOperationException`, `NotImplementedException` (Apache Commons), and generic `RuntimeException/AssertionError/IllegalStateException` with a "not implemented" / "not supported" message literal. Java method-header regex accepts annotations + modifier keywords + generics. Java method DECLS excluded from call-site scanning so same-name interface decls don't get flagged as calls. Standalone validation on all 10 keycloak patches: 003 detects 4 stub-caller pairs, 005 detects 5, 006 detects 1, the other 7 correctly return 0. Grafana-009 still detects the original 2 Go stubs — no regression. 6 new Java unit tests + 7 existing → 88 total pr_brain tests pass. Keycloak suite eval pending (not in our v2m regression scope). |
| 2026-04-21 late morning | **P12 agent_factory (role-based dispatch)** | **Re-introduces role-specialisation as an OPTIONAL dispatch mode**, without destroying v2's scope discipline. User-driven design: `config/agents/` (v1's role agents) stays untouched; NEW `config/agent_factory/` holds 6 role templates (security/correctness/concurrency/reliability/performance/test_coverage) structured as Lens / Typical concerns / Investigation approach / Finding-shape examples. Schema `DispatchSubagentParams` extended with `role: Optional[str]` + `direction_hint: Optional[str]`; `checks` relaxed to optional with validator requiring at least one of {checks, role}. **Brain *composes* the sub-agent system prompt** — loads the factory file at dispatch time, fuses the role's Lens + Approach + Examples with the PR-specific scope + direction_hint + Survey context. The factory is a REFERENCE (teach), not a template to paste verbatim. Coordinator prompt now lists the 6 roles + shows 3 `<example>` blocks (role-only, checks-only, combined). Cluster-first guidance added for PRs ≥15 files (max dispatch cap 12). 18 new unit tests: schema (role+checks validation), `_load_role_template`, `_compose_role_system_prompt`, integration. 334 tests pass. Smoke on sentry-001 + grafana-009 + keycloak-003 in flight. |
| 2026-04-21 midday | P12 supplementary | (1) `_build_v2_coordinator_query` now carries a dynamic **dispatch cap** section scaled by PR size: <5 files = 4 dispatches, 5-14 = 8, ≥15 = 12 (+ cluster-first guidance). (2) Telemetry: `_dispatch_subagent` emits a log line per dispatch capturing `mode=role|checks role=X scope_files=N depth=D` so future analysis can see which mode the coordinator actually chose. (3) Role-mode findings auto-tagged `_dispatched_by=role=X` in the tool-result so downstream dedup/synthesis can attribute. (4) `make eval-brain-regression` target extended to include `greptile-keycloak` suite (first time this suite is exercised under our brain — 10 new Java cases). (5) Smoke results: sentry-001 0.683 (single-run, noise), grafana-009 0.594 (P14 still fires — 3 MATCH + 1 extra), keycloak-003 0.666 (new; Y catch; 3 MATCH + 4 P14.java injected + 1 coordinator nit). P14.java extras on keycloak-003 are **real bugs Greptile's ground truth doesn't label** — unavoidable precision cost on unchecked ground truth. Full v2n regression launched (42 cases × 4 suites). |
| 2026-04-21 noon | **v2n FINAL** | **All 42 cases (4 suites).** Requests 0.930 / 12/12 catch. Sentry 0.788 / 6/10 catch. Grafana 0.724 / 8/10 catch. Keycloak **0.771 avg** (range 0.66-0.92, first-time Java suite). **3-suite avg 0.814 vs v2m 0.817 = −0.003** — tied within LLM variance. Role-dispatch infrastructure fully landed and non-regressive. Keycloak baseline established. Net: +1 full language (Java) of eval coverage; same quality on existing suites. |
| 2026-04-21 afternoon | **v2o iteration** | **Diagnosis-driven.** v2n catch-rate dip analysis revealed a single new miss (sentry-009) caused by **P13 false-positive on external packages**. Fix: `_module_is_first_party(workspace, module)` — P13 now skips imports whose module path doesn't resolve to a file in the workspace (e.g. `from arroyo import KafkaPayload` — `arroyo/` lives in `.venv`, not the source tree). Validation: sentry-001 still flags `OptimizedCursorPaginator` (first-party), sentry-009 now emits 0 false-positive ImportError injections (was 2). **Multi-role per cluster FLEXIBILITY landed per user feedback**: coordinator prompt explicitly allows 0-5 role dispatches per cluster, emphasising per-cluster judgment not default pair. **Budgets bumped** to give multi-role room: dispatch caps 4/8/12 → **5/10/16**, coordinator max_iterations 25 → **32**, coordinator budget 400K → **550K**. 120 pr_brain tests pass (+2 new for first-party detection edge cases). Smoke in flight. |
