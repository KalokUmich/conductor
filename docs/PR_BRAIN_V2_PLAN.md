# PR Brain v2 — Refactor Plan

Dated: 2026-04-19. Written after shipping Phase 9.15 (Fact Vault) + 9.18
(tree-sitter hardening). This is the plan for the **next major effort**:
retiring the fixed 7-agent swarm in `pr_brain.py` and replacing it with
a coordinator-pattern loop where Brain plans investigations and
Haiku workers answer narrow checks.

## Why now

Two things shipped that were blockers for v2:

1. **Fact Vault (9.15)** — sub-agents now share short-term memory. v2's
   `dispatch_subagent` contract relies on multiple concurrent sub-agents
   reusing each other's `grep` / `read_file` results. Without the vault,
   v2 would have the same stampede problem that killed sentry-006 scan.
2. **Scan hardening (9.18)** — the workspace scan is no longer a 24-min
   failure mode. Brain can afford to call `_ensure_graph` during its
   survey phase without risking the whole review going off a cliff.

Additionally, sentry-007 exposed a review-quality failure that v1's
fixed-swarm architecture is structurally unable to fix: three of seven
role-shaped Haiku agents independently hallucinated a null dereference
in `sync.py:141` that was provably absent (short-circuit ternary).
Fixed-role dispatch does not let Brain direct attention to the files
where the real bugs live — it only decides which 7 roles to run. v2's
scope-per-dispatch model fixes this at the architectural level.

## What v1 does (today)

`PRBrainOrchestrator.run_stream` in `backend/app/agent_loop/pr_brain.py`
executes a 6-phase deterministic pipeline:

```
Phase 1 (deterministic): parse_diff → classify_risk → prefetch_diffs → impact_graph
Phase 2 (LLM fan-out):   7 agents — correctness, correctness_b, security,
                         reliability, concurrency, test_coverage, performance
                         — each gets the full diff + the whole impact context,
                         each decides what to investigate, each classifies severity
Phase 3 (deterministic): evidence_gate → post_filter → dedup → rank
Phase 4 (LLM):           standalone arbitrator rebuts each finding
Phase 5 (deterministic): merge_recommendation
Phase 6 (LLM):           synthesis = final judge
```

## What v2 does

A 5-phase coordinator loop. Brain **plans** investigations from the
diff + survey; sub-agents **execute** narrow checks; Brain **classifies
and synthesises** all findings at the end.

```
Phase 1: Survey     Brain (Sonnet) reads diff + uses read-only tools
                    to map change points + risk surface. ≤100K tokens.
                    For each changed region, asks:
                      - what's the intent?
                      - what class of failure if wrong?
                      - what assertions rule those failures out?

Phase 2: Plan       Brain decomposes the survey into concrete
                    investigations. Each investigation is one
                    dispatch_subagent call with:
                      - narrow scope (≤3 files)
                      - exactly 3 falsifiable checks
                      - success_criteria
                      - budget
                      - model_tier (usually Haiku, sometimes Sonnet)

                    Hard invariants (prevent under-exploration):
                      - ≥1 correctness investigation per PR
                      - auth/crypto/session diffs → mandatory security dispatch
                      - DB migrations → mandatory reliability dispatch
                      - ≤8 dispatches total, ≤3 checks per dispatch
                      - Max recursion depth 2 (Brain=0, worker=1, worker's
                        strong-model verifier=2)

Phase 3: Execute    Parallel dispatch_subagent. Each worker returns:
                      {
                        checks: [{verdict: confirmed|violated|unclear, evidence}],
                        findings: [{severity: null, title, file, line, ...}],
                        unexpected_observations: [{confidence, ...}]
                      }
                    Workers NEVER classify severity. They NEVER recurse.
                    They NEVER investigate outside scope.

Phase 4: Replan     Brain reacts to "unclear" verdicts and high-confidence
                    "unexpected_observations". Up to 2 replan rounds,
                    still within the max-8-dispatches budget.

Phase 5: Synthesis  Brain dedups findings across all dispatches, classifies
                    severity using the 2-question rubric (provable? +
                    blast radius?) with full cross-cutting context, and
                    emits the final review. The standalone arbitrator is
                    folded into this phase — Brain may fork a strong-model
                    verifier (9.16 Forked Agent Pattern) for findings
                    whose evidence is thin.
```

## Concrete code changes

### Backend: `backend/app/agent_loop/pr_brain.py`

- **`PRBrainOrchestrator.__init__`** — add `meta_skill` parameter. When
  set, the orchestrator runs the v2 coordinator loop. When unset,
  falls back to the v1 fixed-swarm pipeline (rollback safety).
- **`_survey()` method (new)** — Brain LLM call with read-only tools,
  returns structured survey output (change points + risk notes per
  change).
- **`_plan()` method (new)** — LLM call, consumes survey, emits a list
  of `Investigation(scope, checks, success_criteria, budget, model_tier)`.
- **`_execute()` method (new)** — wraps `dispatch_subagent` calls in
  parallel, collects worker responses.
- **`_replan()` method (new)** — decides whether to dispatch more
  investigations based on unclear + unexpected; bounded by dispatch
  budget + round count.
- **`_synthesize_v2()` method (new)** — dedup + severity classification +
  final review; replaces the current `_arbitrate` + synthesis split.
- **v1 path preserved** — current `run_stream` keeps working when
  `meta_skill` isn't set. This is how we ship Checkpoint A without
  breaking production Azure DevOps reviews.

### Backend: `backend/app/agent_loop/brain.py`

- **`AgentToolExecutor` — new `dispatch_subagent` tool**. Shape:
  ```python
  dispatch_subagent(
      scope: list[str],        # file paths, max 3
      checks: list[str],       # falsifiable questions, exactly 3
      success_criteria: str,   # what "confirmed" means
      budget: int,             # max iterations
      model_tier: str,         # "haiku" | "sonnet"
  ) -> SubagentResponse
  ```
- Returns the new schema (checks + findings + unexpected).
- Tracks dispatch depth via a ContextVar so recursive calls can be
  rejected at the executor layer (hard invariant 4).

### Config: `config/prompts/`

- **`pr_subagent_checks.md`** (new) — sub-agent system prompt.
  Detection-only, no severity classification, verify-existence rule,
  scope restriction, exit with the new schema.
- **`pr_brain_coordinator.md`** (new) — Brain meta-skill. Describes
  the 5-phase loop, the dispatch contract, the hard invariants, the
  severity rubric. This is what lets Brain autonomously plan
  investigations.

### Config: `config/agents/*.md`

- Top-of-file banner added: "Reference material for PR Brain v2 —
  Brain composes investigations fresh per-PR rather than copying these
  broad framings." In v1 they remain the active dispatch targets.

### Tool schemas: `backend/app/code_tools/schemas.py`

- Add `DispatchSubagentParams` Pydantic model.
- Register in `TOOL_DEFINITIONS` + `TOOL_METADATA`.
- Only exposed to Brain, not to sub-agents (hard invariant: no
  recursion).

## Rollout — two checkpoints

### Checkpoint A (Sprint 16/17) — primitive + parallel availability

Lands `dispatch_subagent` + the new sub-agent schema alongside the
existing fixed swarm. Both paths work; v2 is opt-in via config flag.

- `dispatch_subagent` tool + schema + Pydantic params
- `pr_subagent_checks.md` sub-agent skill
- Brain synthesize path branches: if any finding carries
  `severity: null` (new-schema worker), classify via Brain rubric;
  else pass through the v1 finding's severity.
- Verify-existence rule wired into the new sub-agent skill
- Side-by-side eval: `dispatch_subagent`-only vs fixed-swarm on the 12
  requests cases + Greptile sentry subset. Brain-driven severity
  classification validated against existing severity judgments.

Acceptance: `dispatch_subagent` works end-to-end; Brain severity
classification matches or exceeds fixed-swarm `severity_accuracy`
on 12 requests cases.

### Checkpoint B (Sprint 18) — switch default, retire swarm

- `pr_brain_coordinator.md` becomes Brain's default system prompt for
  PR review flow (Brain plans dispatches itself)
- Hard invariants enforced in code (min correctness, trigger patterns,
  max 8 dispatches, max depth 2)
- `config/agents/*.md` get reference-only banner
- Standalone arbitrator prompt retired; logic folded into synthesize
- Fixed swarm → fallback only, wrapped with a deprecation log

Acceptance:
- composite within ±1pp of Checkpoint A
- severity_accuracy 0.583 → 0.75+
- judge avg 2.2 → 3.0+
- token cost -30%+ vs fixed swarm

## Test plan

### Unit tests (added alongside each change)

- `test_dispatch_subagent_scope_enforced.py` — worker rejects reads
  outside declared scope
- `test_dispatch_subagent_no_severity.py` — worker output has
  `severity: null` on every finding
- `test_dispatch_subagent_verify_existence.py` — worker refuses to
  flag logic on symbols it didn't grep/find_symbol first
- `test_brain_severity_classification.py` — Brain's 2-question rubric
  applied to sample finding batches, verdicts stable
- `test_pr_brain_v2_invariants.py` — min correctness dispatch, auth
  triggers security, migrations trigger reliability, max 8 dispatches,
  max depth 2

### Integration / eval

- Existing 12 requests cases — regression floor (composite ±2pp)
- Greptile sentry-001..007 — bug detection recall vs baseline
- Greptile grafana / discourse subset — broader language coverage
- Langfuse traces: dispatch graph visible per PR, ≤8 dispatches, depth
  never exceeds 2

### Parity tests

- `dispatch_subagent` is backend-only (no TS side), but sub-agent tool
  access surface must stay parity-covered. Re-run the 160-test parity
  suite after each checkpoint.

## Dependencies

- **9.15 Fact Vault** — ✅ shipped. Required because multiple concurrent
  sub-agents will hit the same grep / read_file queries; vault dedups.
- **9.18 subprocess parse pool** — ✅ shipped. Required because Brain's
  survey phase calls `_ensure_graph`; this must be bounded on
  pathological TSX.
- **9.16 Forked Agent Pattern** — Checkpoint B prerequisite. Brain's
  merged arbitrator forks strong-model verifiers for weak-evidence
  findings. Without it, each verifier would pay a full fresh-dispatch
  prompt cache write.

## Order of operations

1. Write `dispatch_subagent` Pydantic schema + tool stub (1–2 hrs)
2. Write the sub-agent skill `pr_subagent_checks.md` (1–2 hrs)
3. Wire into `AgentToolExecutor` with scope enforcement + depth guard
4. Unit tests for scope, schema, verify-existence
5. Brain synthesize path — severity classification branch
6. **Checkpoint A eval** — 12 requests + sentry subset, compare
7. `pr_brain_coordinator.md` skill
8. Brain v2 orchestrator — `_survey` / `_plan` / `_execute` / `_replan`
   / `_synthesize_v2`
9. Hard invariant enforcement + unit tests
10. **Checkpoint B eval** — full comparison, cost, severity

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| v2 misclassifies severity worse than v1 | Checkpoint A gating eval — if severity_accuracy drops, we don't flip the default |
| Brain's plan phase hallucinates investigations | Hard invariants cap dispatch count; unit tests verify specific trigger patterns always fire |
| Replan rounds explode budget | Max 2 replan rounds + total 8-dispatch cap enforced at the executor |
| Workers break the "no severity" contract | Pydantic schema rejects non-null severity; tests assert |
| v1 users on main break during rollout | `meta_skill` config flag keeps v1 path alive until Checkpoint B |

## Not in scope

- Changing any other pipeline (summary, Teams bot, Jira agent) — v2 is
  PR-review-only for now
- Frontend changes — the UI reads the same `ReviewResult` shape
- Webhook changes — Azure DevOps still calls
  `POST /api/integrations/azure_devops/webhook`, sees the same response
