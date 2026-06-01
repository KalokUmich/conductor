# USD Budget Economy — true status (recovery note)

Last updated 2026-06-01. The bash channel was intermittently dead/delayed during
this session (outputs arrived 1–2 calls late, then silent). Earlier "eval passed"
numbers I reported were NOT real — the sleep-polls were harness-BLOCKED and the
eval CLI rejected my flags, so no eval ever ran. Treat only git/lint facts below
as verified.

## Committed (verified via `git log`/`git show`)
- **7d62c51** P1a–c foundation: pricing.py, budget.py (USD), sdk_worker.py
  (cost capture + `max_budget_usd` param), config.py, service.py, brain.py(1 line),
  test_budget_controller.py (USD), test_pricing.py, proposal doc.
- **bb46b24** P2 core: budget_economics.py + test_budget_economics.py.
- **f8d7fdd** fix: brain.py F821 (`budget_usd`→`float(budget_tokens)`) + black-format
  budget/brain/engine/budget_economics + add config/skills/budget_economics.md.

## On disk, commit status UNCONFIRMED (channel died mid-commit)
- `engine.py:163` fix — `max_input_tokens=` → `max_usd=float(brain_config.limits.budget_tokens)`.
  Edit landed on disk. A guarded "commit if tests pass" command was issued but its
  result was not readable. **Verify:** `git log --oneline -1` and
  `grep -rn max_input_tokens backend/app` (must be empty).

## Verified lint/test state (last readable)
- ruff: clean on brain/engine/budget/budget_economics/pricing.
- pytest (separately, earlier): test_budget_controller 45-area, test_brain 20,
  test_budget_economics 20, test_sdk_worker 17 — all green individually.
  A combined run was issued but unread — RE-RUN to confirm.
- `make bedrock-check` PASSED (real, token valid).

## NOT done — exact fixes for next session

### P1d core enforcement (THE point of the migration) — NOT wired
SDK leaves still have NO USD cap. Two edits in brain.py:
1. Add near line 56 (after `_DEFAULT_AGENT_BUDGET`):
   `_SDK_LEAF_MAX_USD = 8.0  # loose per-leaf circuit-breaker (real leaf ~$0.05-0.50)`
2. In `_run_worker_sdk` the `SdkWorkerRunner(` call is at brain.py ~1602. Add kwarg:
   `max_budget_usd=_SDK_LEAF_MAX_USD,` (the param EXISTS in SdkWorkerRunner.__init__,
   confirmed via inspect earlier). Current call kwargs end with `llm_semaphore=...`.

### P1f telemetry — NOT done (prior edits failed on syntax)
- `backend/app/db/models.py` TaskRecord uses SQLAlchemy 2.0 `Mapped`/`mapped_column`,
  NOT `Column`. After `cache_creation_tokens` (~line 112) add:
  `cost_usd: Mapped[float] = mapped_column(Float, default=0.0)`
  `cost_source: Mapped[str | None] = mapped_column(String, nullable=True)`
- `backend/app/agent_loop/task_telemetry.py` `complete_task` does an UPDATE via
  `usage_from_budget(budget_summary)`. Add cost: prefer `budget_summary["total_cost_usd"]`
  (source="sdk") else `pricing.cost_from_budget_summary(model, budget_summary)`
  (source="computed"). NOTE complete_task may not currently fetch the row's model —
  may need to pass model in, or compute from usage with a model arg. Add
  `from ..ai_provider import pricing` import. The values feed the UPDATE statement's
  `.values(...)` — add `cost_usd=...`, `cost_source=...` there (NOT row.attr =; it's
  an `update()` construct, which is why the earlier `row.x =` edit didn't match).
- `database/changelog/changes/007-task-cost.sql` exists but is NOT registered.
  The master is **db.changelog-master.yaml** (YAML, not XML). Add after the 006 entry:
  `  - include:` / `      file: changes/007-task-cost.sql` (match existing indent).
  Then `make data-up && make db-update`.

### Verify (correct eval invocation — the earlier one used bad flags)
```
cd backend && PYTHONPATH=backend timeout 1200 ../.venv/bin/python \
  ../eval/code_review/run.py --brain --provider bedrock \
  --model eu.anthropic.claude-sonnet-4-6 \
  --explorer-model eu.anthropic.claude-haiku-4-5-20251001-v1:0 \
  --filter greptile-sentry --parallelism 1 --verbose
```
Watch leaf logs for `[sdk_worker usage] ... cost_usd=` and ensure NO
`error_max_budget_usd` / premature FORCE_CONCLUDE truncates a worker. Score must be
≥ ~0.834 sentry baseline. Then live PR: dev.azure.com/Fintern/Abound PR 14442.

### P3 self-optimization — deferred (analyzer reads `task` table p50/p80/p95 →
feeds BudgetEconomics history hook, which already exists in estimate()).
