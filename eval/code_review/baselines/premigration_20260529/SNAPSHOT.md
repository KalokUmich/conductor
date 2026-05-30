# Pre-migration baseline snapshot — agent-SDK migration bar

> Captured: 2026-05-29 (run finished 23:41 UTC) · Commit: `aa316f9` (current `main`)
> Purpose: the **meet-or-exceed bar** for the Bedrock+Claude+SDK refactor
> (`docs/agent-sdk-hybrid-worker-design.md` §11.2 / §12.4). After migration,
> code review (PR Brain v2) must match or exceed these numbers.

## Run config

- **Brain (coordinator): Sonnet** — `eu.anthropic.claude-sonnet-4-6` (Bedrock, eu-west-2)
- **Explorer (sub-agents): Haiku** — `eu.anthropic.claude-haiku-4-5-20251001-v1:0`
- Path: `PRBrainOrchestrator` v2 (`--brain`), full graph tools ON (venv python: networkx 3.6.1 + tree-sitter)
- Suites run **serially** (OOM discipline); cases at `--parallelism 3`
- LLM judge enabled

## Results

Metric columns: Catch / Recall / Prec / Sev / Loc / Rec / Ctx / **Composite**

| Suite | Lang | Cases | Catch | Recall | Prec | Sev | Loc | Rec | Ctx | **Comp** |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| planted `requests` | — | 12 | 12/12 (100%) | 1.000 | 0.917 | 0.625 | 1.000 | 1.000 | 0.958 | **0.923** |
| greptile sentry | Python | 10 | 5/10 (50%) | 0.808 | 0.840 | 0.633 | 0.608 | 1.000 | 0.517 | **0.758** |
| greptile grafana | Go | 10 | 9/10 (90%) | 0.708 | 0.722 | 0.563 | 0.838 | 1.000 | 0.667 | **0.727** |
| greptile keycloak | Java | 10 | 10/10 (100%) | 0.850 | 0.721 | 0.442 | 0.925 | 1.000 | 0.633 | **0.764** |

**Greptile real-PR aggregate (Py+Go+Java, 30 cases): catch 24/30 = 80% · avg composite 0.750**

(cal.com / TS and discourse / Ruby intentionally skipped this run.)

## Headline bars for the migration

- **Planted-bug composite ≥ 0.923** (was 0.926 on the stale 2026-04-10 baseline → no regression on current main).
- **Greptile catch ≥ 80%** (24/30) across Python/Go/Java; per-repo: sentry 50%, grafana 90%, keycloak 100%.

For reference, Greptile's own published July-2025 catch rates (different harness, not directly comparable):
Greptile 82% · Cursor 58% · Copilot 54% · CodeRabbit 44% · Graphite 6%.

## Caveats

- **Catch-rate definition is strict**: a catch requires title+file+**line** all matching the expected finding. Several misses found the right *file* but a different line range (e.g. sentry-001), so catch undercounts "found the bug." Composite is the more forgiving signal.
- **Severity is the weakest axis** everywhere (0.44–0.63) — calibration headroom independent of the migration.
- **Existence-check worker 60s timeout** fired on a few cases (by design, v2u); P13 facts were already persisted so the coordinator proceeded. Not a failure.
- Creds were SSO **temporary** (`ASIA…`, account 533267248474) — they expire; re-`aws sso login` before re-running.
- Per-case variance exists at `--parallelism 3`; treat single-case scores as indicative, suite aggregates as the bar.

## Artifacts (this dir)

- `planted_requests.{json,log}`, `greptile_sentry_python.{json,log}`,
  `greptile_grafana_go.{json,log}`, `greptile_keycloak_java.{json,log}`
- Reproduce: `bash eval/code_review/run_premigration_baseline.sh` (uses venv, Sonnet brain)
