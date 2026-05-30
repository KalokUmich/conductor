# Agent-SDK refactor — execution log & protocol

> Parent branch: `refactor/agent-sdk-migration` (cut from `main` @ `de603e8`, 2026-05-30).
> Design: `docs/agent-sdk-hybrid-worker-design.md` (DECIDED: Bedrock-only + Claude-only + SDK, retire Langfuse incl. DB).
> This file is the live status board. Update it at every step boundary so any machine can resume.

## The loop (one step = one child branch)

```
for each step S:
  0. PRE-FLIGHT: bash scripts/refactor/check_creds.sh   # MUST pass if S has a Bedrock test
       └─ exit 1  → STOP. Ask user to `aws sso login` + refresh
                    config/conductor.secrets.local.yaml. Resume only after confirmed.
  1. git checkout refactor/agent-sdk-migration
  2. git checkout -b refactor/step-NN-<slug>
  3. make the change for S ONLY (small, single-purpose)
  4. write/extend tests for S
  5. run the gate (§ gate matrix) — re-run creds check before any eval
  6. gate red → fix on child, repeat
  7. commit on child (one coherent commit)
  8. merge child → parent (--no-ff), delete child
  9. update this log, go to next step
```

Rules: one step = one purpose; never merge red; parent always green; structural
steps (05–06) human-reviewed at the merge boundary. Parent → `main` only at the
end / safe milestones.

## Credential gate (user rule, 2026-05-30)

Before **every test that may call Bedrock** (any eval, PR-brain, tool e2e), run
`scripts/refactor/check_creds.sh`. If it exits non-zero, **HALT and ask the user
to refresh the token** in `config/conductor.secrets.local.yaml`; resume only
after they confirm. The token is SSO-temporary (`ASIA…`) and expires in hours.

## The bar (meet-or-exceed, from current main)

`eval/code_review/baselines/premigration_20260529/SNAPSHOT.md`:
- planted `requests` (12): **composite ≥ 0.923**, catch 12/12
- greptile (Py+Go+Java, 30): **catch ≥ 80%** (sentry 50% · grafana 90% · keycloak 100%), avg composite 0.750

## Gate matrix (what each step MUST pass before merge)

| Step touches… | Gate |
|---|---|
| Config / provider deletion | `make test` + `make test-parity` + `make lint-check` + `make typecheck-strict` |
| Any prompt (prompts.py, agents, agent_factory, skills) | standard gate **+ code-review eval + agent-quality eval** (creds-gated) |
| Brain / dispatch / pr_brain | standard gate **+ PR-review e2e + tool functionality + code-review eval** (creds-gated) |
| SDK worker / executor wiring | standard gate **+ spike checks (§7): vault hit, WS tool proxy, return-contract** (creds-gated) |
| Observability / DB tables | `make test` + Liquibase up/rollback |

## Step plan & status

| # | Step | Branch | Bedrock test? | Status |
|---|---|---|---|---|
| — | Baseline capture | (on main) | yes | ✅ done — `premigration_20260529` |
| — | Scaffolding (cred gate + this log + parent branch) | parent | no | ✅ done |
| 01 | DB telemetry tables + remove Langfuse DB plumbing | `refactor/step-01-db-langfuse` | no | ✅ done (backend 1265 passed; Liquibase up/rollback ✓) |
| 02 | Config collapse → Bedrock+Claude (4 models / 2 providers) | `refactor/step-02-config` | no | ⬜ pending |
| 03 | Provider dead-code removal (OpenAI provider, tool-repair, schema-sanitize, enable_thinking; simplify resolver) | `refactor/step-03-provider` | no | ⬜ pending |
| 04 | Observability swap — delete `@observe`/`track_generation`; wire OTEL + new tables | `refactor/step-04-otel` | smoke | ⬜ pending |
| 05 | **SDK worker spike (GATE)** — prove 4 seams (§7) | `refactor/step-05-spike` | yes | ⬜ pending |
| 06 | SDK worker integration behind `brain.py:1323` | `refactor/step-06-sdk-worker` | yes | ⬜ pending |
| 07..N | Prompt rewrite for Claude+preset (one file/group per child) | `refactor/step-NN-prompt-*` | yes | ⬜ pending |
| final | Code-review eval gate (Task B) — iterate to ≥ bar | parent | yes | ⬜ pending |

## Decisions locked (from design §11/§12)

D1=go · D5=maximal cleanup (explorer tier goes Claude) · D6=retire Langfuse incl. DB.
~5,200 LOC net deletion expected. Steps 01–04 + 07..N are workflow/loop-safe;
05–06 are structural → human review at merge.

## Session journal

- 2026-05-30 — parent branch cut; cred gate built; **cred check FAILED (SSO token expired)** → user refreshed (fixed a missing closing-quote on session_token), creds green.
- 2026-05-30 — **Step 01 done.** Added `005-telemetry.sql` (iteration_token_usage + agent_transcript); removed all Langfuse DB/infra (init-db.sql, langfuse compose, Makefile targets, env vars). Gate: backend pytest **1265 passed / 21 skipped / 0 fail** (incl. tool-parity) + Liquibase up/rollback clean.
  - **GOTCHA logged:** `make test` hung ~7h on `test_local_tools_parity::test_get_dependencies` — a tree-sitter forkserver (`app.repo_graph.parser`) deadlock on WSL2 (`Dl` uninterruptible state), unrelated to the change. Root cause of the *silent* hang: `pytest-timeout` was not installed in the venv. **Fix: added `pytest-timeout` to `backend/requirements.txt`; always run pytest with `--timeout=180 --timeout-method=thread` + a shell `timeout` backstop.** Extension/webview JS suites skipped for Step 01 (zero TS changes).
- Next: Step 02 (config collapse → Bedrock+Claude). Recon done (provider/config coupling inventory).
