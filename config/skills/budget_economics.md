---
name: budget_economics
description: Pre-flight USD budget consulting. Call estimate_task_budget before fanning out to sub-agents so each leaf and the whole task get a loose dollar ceiling. Mandatory for PR review and business/domain tasks; LLM-judged for everything else.
---

# Budget economics — consult before you spend

You are the coordinator. Before you dispatch sub-agents, you decide how much the
task is *allowed* to cost. You do this by calling **`estimate_task_budget`**,
which returns a **budget plan**: a total USD ceiling for the task and per-leaf
caps for the workers you'll dispatch. The plan is enforced downstream — each SDK
leaf runs under its `max_budget_usd`.

## The mental model

- **Caps are loose safety circuit-breakers, NOT targets.** Real spend is tiny — a
  heavy PR review costs about **$1** across ~9 leaves; one leaf is **$0.05–0.50**.
  The caps exist only so a runaway loop can't cost hundreds. A normal worker
  finishes far under its cap. Never treat the cap as a budget to "use up", and
  never cut a worker's depth to stay under a cap that's 10–100× its real cost.
- **Hard anchor:** a **2000-line PR ⇒ $50 total ceiling** (PRs above ~2200 lines
  aren't reviewed at all). Smaller PRs scale down linearly to a $5 floor.

## When to consult

- **PR review → ALWAYS.** Pass `query_class="pr"` and `diff_lines`. The plan
  scales the ceiling to the diff and sets per-leaf caps for the dimension/verify
  workers.
- **Business / domain-logic understanding → ALWAYS.** These fan out to several
  explorers and deserve a real plan. Pass `query_class="business"` (and
  `expected_leaves` if you know your fan-out).
- **Everything else → YOUR CALL.** If the task will fan out to multiple or
  expensive sub-agents, consult with `query_class="generic"`. If it's a trivial
  single-shot job (a Jira triage, a summary, a splitter pass), **skip the consult**
  — pass the `sub_type` only if you do call, and otherwise just take the cheap
  default. Don't pay consult overhead on throwaway work.

## How to use the plan

1. Read `total_cap_usd` as the whole-task ceiling and `per_leaf_max_usd` as the
   safety cap for each worker you dispatch.
2. Dispatch normally. The caps ride along automatically; you don't pass dollars
   to each `dispatch_*` call.
3. **Degrade, don't abort.** If you're told budget is running low (a WARN signal),
   prefer softening — drop a dispatch's `model_tier` from `strong` to `explorer`,
   or trim `max_turns` — over hard-stopping a worker mid-investigation. A loose
   cap should almost never fire; if it does, the task was genuinely runaway.

## Reading the rationale

The plan includes a `rationale` string explaining *why* it chose those numbers
(e.g. "PR 2000 lines → total $50.00 … ~5 leaves"). It teaches, it doesn't just
command — surface it in your reasoning so the numbers are auditable.
