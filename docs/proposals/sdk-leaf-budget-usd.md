# Proposal: close the SDK-leaf token-budget gap via `max_budget_usd`

## Problem (recap)

The Brain allocates each sub-agent a **token budget** (60%-of-pool rule, capped
50K–800K, `brain.py` `BrainBudgetManager`). For **in-house** coordinators that
budget is hard-enforced (`BudgetController` → `FORCE_CONCLUDE` at 90%). For
**SDK leaf workers** (`SdkWorkerRunner`) the token budget is **computed and
logged but never transmitted** — `ClaudeAgentOptions` only gets `max_turns`. So a
leaf is bounded only by iteration count; a token-heavy leaf can overspend.

## What the SDK offers (confirmed)

- `ClaudeAgentOptions.max_budget_usd: float | None` (types.py:1659) — *"Maximum
  cumulative cost in USD before the session stops."* → CLI flag `--max-budget-usd`
  (subprocess_cli.py:262).
- `ResultMessage.total_cost_usd: float | None` (types.py:1879) — cost actuals.
- Also: `task_budget={"total": N}` → `--task-budget` (semantics unconfirmed),
  `max_tokens` (per-turn cap, not cumulative — not what we want).

There is **no** `max_tokens`-cumulative / token-budget option. USD is the only
cumulative spend lever the CLI exposes.

## The scheme (user's idea: token budget → USD)

Convert each leaf's existing **token budget** into a **USD cap** using a
per-model price table, and pass it as `max_budget_usd`:

```
max_budget_usd = (budget_tokens / 1e6) * blended_price_per_1M(model)
```

where `blended_price_per_1M` comes from a small table keyed by model tier:
```
sonnet : in $3.00 / out $15.00 per 1M   (eu.anthropic.claude-sonnet-4-6)
haiku  : in $0.80 / out $4.00  per 1M   (eu.anthropic.claude-haiku-4-5)
```
Blend assumption: budget is tracked in *input* tokens today, and output is a
small fraction of input for our read-heavy leaves, so a conservative cap can use
the input price (slightly generous) or input + an output-headroom factor (e.g.
1.2×). Exact blend is a knob, not a blocker.

A token→USD helper belongs next to the price table — likely a new
`backend/app/ai_provider/pricing.py` (none exists today; grep found no pricing
table anywhere). `sdk_worker._build_options` then sets `max_budget_usd` from the
runner's `budget_tokens` (which must be threaded in — currently the runner isn't
even given the token number).

## ⚠️ THE GATING UNKNOWN — must verify before building

`max_budget_usd` is enforced **inside the CLI's own cost accounting**, which uses
the CLI's built-in price table keyed by **model id**. Our Bedrock ids look like
`eu.anthropic.claude-sonnet-4-6`. **If the CLI doesn't recognise that id, its
running cost stays $0 and the cap NEVER fires — a silent no-op.**

**Empirical test (run before any code):** a one-shot SDK `query` on Bedrock,
inspect `ResultMessage.total_cost_usd`:
- **non-zero** → the CLI prices Bedrock ids; `max_budget_usd` will enforce. Build the scheme.
- **None / 0.0** → CLI can't price Bedrock; `max_budget_usd` is a no-op. Fall back (below).

(scripts/sdk_smoke.py already drives a leaf end-to-end; extend it to print
`total_cost_usd` from the ResultMessage.)

## Fallback if the CLI can't price Bedrock

If `total_cost_usd` is 0/None on Bedrock, `max_budget_usd` is useless and we
enforce **ourselves**, two options:

1. **Token→turns proxy (simplest):** we already get `usage` per ResultMessage.
   Tighten `max_turns` derived from the token budget (e.g. assume ~Xk tokens/turn
   → max_turns = budget_tokens / Xk), so a smaller budget ⇒ fewer turns. Coarse
   but real, no new machinery.
2. **Streaming stop-hook (proper):** if this SDK build exposes a per-turn hook
   (the sdk_worker docstring calls a Stop-hook variant "a roadmap experiment"),
   accumulate `usage` across turns and abort when tokens exceed budget. This is
   the true equivalent of the in-house `BudgetController` but inside the SDK loop.

## Independent quick win (do regardless)

Capture `ResultMessage.total_cost_usd` into `SdkAgentResult.budget_summary` and
the `[sdk_worker usage]` log line — even if we don't enforce on it yet, it makes
leaf cost **observable** (and feeds `TaskTelemetryService`, which replaced
Langfuse). Cheap, no behaviour change, and it doubles as the empirical probe.

## Recommendation

1. Run the empirical `total_cost_usd`-on-Bedrock test (gating).
2. Capture cost into telemetry regardless (quick win).
3. If priced → wire `max_budget_usd` from token budget via a `pricing.py` table.
   If not → token→turns proxy now, stop-hook later.
