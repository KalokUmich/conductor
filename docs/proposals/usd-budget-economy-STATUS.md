# USD Budget Economy — status & handoff

Last updated 2026-06-01. (The bash channel was unstable for a long stretch
earlier; trust git/exit-code/count facts over any narrative. Earlier in-session
"eval passed 0.81" numbers were NOT real.)

## DONE — committed & verified

| Commit | Phase | What |
|--------|-------|------|
| `7d62c51` | P1a–c | `ai_provider/pricing.py` (token→USD: tier table, EU +10%, cache 0.1×/1.25×); `budget.py` → USD (`max_usd`, `cumulative_usd`, `usd_usage_ratio`, same 0.6/0.9 signal); `sdk_worker.py` captures `total_cost_usd` + `max_budget_usd` param |
| `bb46b24` | P2 core | `budget_economics.py` — consultant. pr/business/generic; **2000-line PR → $50** anchor; cheap sub-types; loose per-leaf caps; Phase-3 history hook. +20 tests |
| `f8d7fdd` | fix | brain.py F821 fix + black + `config/skills/budget_economics.md` |
| `4949d0b` | P1d | **SDK leaf USD enforcement**: `_SDK_LEAF_MAX_USD=8.0` → `SdkWorkerRunner.max_budget_usd`; engine.py pool-seed fixed off removed `max_input_tokens` |
| `03acd8b` | P1f | `task.cost_usd`/`cost_source` columns (migration applied, live in PG); `complete_task` stores SDK-authoritative cost |
| `60d8d11` | eval fix | code-review eval `create_provider` now resolves the **bearer token** via shared `bedrock_env()` (was static-keys-only → UnrecognizedClientException on every call → bogus composite=0.000) |
| `8b271cb` | P2 routing | PR Brain does a **mandatory pre-flight consult** (`BudgetEconomics.estimate('pr', diff_lines, file_count)`), logs plan + emits `budget_plan` event; computed `per_leaf_max_usd` flows via `BrainExecutorConfig.leaf_max_usd` → each SDK leaf's `max_budget_usd` |

**`max_budget_usd` IS enforceable** (earlier doubt was wrong): `ClaudeAgentOptions.max_budget_usd`
exists in SDK 0.2.87 → `subprocess_cli.py:262` emits `--max-budget-usd`; the installed
CLI is **2.1.159** and its `--help` lists `--max-budget-usd <amount>` ("stop, returns
`error_max_budget_usd`"). So the per-leaf cap is real, not a no-op.

**Test state**: budget/pricing/economics/brain/sdk/pr_brain/config/db all green
(191 in the last combined run). ruff + black clean. `make bedrock-check` PASS.
Migration 007 applied. No `max_input_tokens` left in `backend/app`.

## VERIFICATION (task #21)
- **Hard case `greptile-sentry-007` PASSED** (log /tmp/hard007.log):
  composite **0.885**, recall **1.00**, catch **100%**, findings **3**;
  **budget_hardstops=0, force_conclude=0**, crederr=0, traceback=0. Per-leaf
  cost **$0.11–$0.22** across 4 leaves — i.e. real spend is 16–70× under the loose
  $8 cap, proving the cap never throttles a normal worker. ✅ THE key gate (USD
  budget does NOT hard-stop subagents) is met.
- **Full sentry suite IN FLIGHT** (bg b9xcff861, log /tmp/sentry_full2.log) on the
  P2-routing code. Early signals: crederr=0, budgeterr=0, and the consultant fires —
  `[PR Brain v2] Budget plan: {'query_class':'pr','total_cap_usd':5.0,
  'per_leaf_default_usd':1.67,'per_leaf_max_usd':2.5,'expected_leaves':3}` on a
  138-line PR (floor $5; loose per-leaf $2.50 vs ~$0.15 real). Awaiting suite AVG
  vs ~0.834 baseline.
- Then live PR review: `dev.azure.com/Fintern/Abound/_git/abound-server/pullrequest/14442`,
  confirm total `task.cost_usd` ≪ $50.

## P3 DONE (committed `969ef08`)
- `budget_analyzer.py` — reads `task.cost_usd` per agent, p50/p80/p95 (drops <5
  samples, filters zero-cost in-house rows), exposes a `history` provider that
  `BudgetEconomics.estimate()` blends toward p80. Best-effort/read-only; no DB →
  pure policy. +14 tests. Wire `refresh()` to the `on_task_end` hook or a cron next.

## User-decided design call (AskUserQuestion)
- USD enforcement = **coordinator-level total cap** (chosen) layered on the working
  per-leaf SDK cap. Per-leaf cap already wired; the *total* task cutoff (track
  cumulative leaf `total_cost_usd`, stop dispatching the next wave at the plan's
  `total_cap_usd`) is the remaining piece — implement after eval validation so the
  control-flow change is verified separately.

## NOT done (lower priority)
- **Total-cap cutoff** (above) — coordinator accumulates leaf cost, stops at
  `budget_plan.total_cap_usd`. Degrade-don't-abort: on WARN, downgrade tier /
  trim max_turns before refusing to dispatch.
- **P1e** cosmetic rename `budget_tokens`→`budget_usd` across `workflow/models.py`
  + brain YAMLs + agent frontmatter. Interim `float(budget_tokens)` casts keep the
  in-house USD cap a harmless no-op (loop bounded by max_iterations); only the SDK
  leaf cap is "real", which is the correct behaviour.
- **P3** self-optimization analyzer: read `task` p50/p80/p95 `cost_usd` per
  (query_class, agent) → feed the `history` hook `BudgetEconomics.estimate` already
  supports.

## Correct eval invocation
```
cd backend && PYTHONPATH=backend timeout 2700 ../.venv/bin/python \
  ../eval/code_review/run.py --brain --provider bedrock \
  --model eu.anthropic.claude-sonnet-4-6 \
  --explorer-model eu.anthropic.claude-haiku-4-5-20251001-v1:0 \
  --filter greptile-sentry --parallelism 1 --verbose
```
Watch (count-only): `error_max_budget` (must be 0), `cost_usd=` per leaf, `composite=`.
