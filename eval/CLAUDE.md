# Eval CLAUDE.md

Three eval suites. See `eval/README.md` for full docs.

```
eval/
├── code_review/        12 requests + 10 sentry + 10 grafana + 10 keycloak cases
│                       (Greptile-style composite scorer + LLM Judge)
├── agent_quality/      Agentic loop answer quality vs baselines
└── tool_parity/        Python vs TS tool output comparison
```

## Fresh-machine setup — the greptile bases are GITIGNORED

**`git pull` is NOT enough to run the greptile suites.** What's tracked vs not:

| Tracked (comes with `git pull`)                          | Gitignored — must regenerate locally                       |
|----------------------------------------------------------|------------------------------------------------------------|
| `cases/greptile_*/cases.yaml` + `manual_cases.yaml` (gold) | `repos/<target>-greptile/` (full fork clones, ~2 GB)       |
| `cases/greptile_*/patches/*.patch`                       | `repos/greptile_bases/<target>/<NNN>/` (base snapshots, ~6 GB) |
| all scorer / importer / runner code                      | `cases/greptile_raw/*.json` (scraped JSON; re-import only)  |

So on any new box (or after a teammate adds cases), run the **one-time setup**
before the first eval — it clones the 5 forks and `git archive`s each case's
merge-base snapshot onto disk:

```bash
make greptile-setup     # clone forks + materialize bases (fresh machine)
make greptile-repair    # re-extract bases --force, reuse clones (fix corruption)
make greptile-repair TARGET=keycloak   # one target only
```

These wrap the underlying scripts (run directly if you prefer):
```bash
cd backend   # so PYTHONPATH picks up app.*
python ../eval/code_review/setup_greptile_dataset.py            # = greptile-setup
python ../eval/code_review/materialize_greptile_bases.py --skip-clone --force  # = greptile-repair
```

Public repos → **no GitHub token** needed. Idempotent. Success looks like
`Done. materialized=50 patches_regenerated=50 ...`. **`make greptile-repair`** is
the fix when an eval case ERRORs on `git apply ... patch does not apply`: the
bases are rebuilt fresh from `git archive merge_base`, so any local base
corruption (e.g. the hardlink-inode class of bug — runner.py breaks hardlinks
before `git apply` to prevent it) is wiped, and it never transfers between
machines. Full detail, the merge-base trick, and the hardlink/atomic-write
design: **`code_review/GREPTILE_BENCHMARK.md`** §2-3, §7-8.

## Commands

```bash
cd backend

# Single-suite code review (PR Brain v2, default; Brain mode is implied)
python ../eval/code_review/run.py --provider bedrock \
    --model eu.anthropic.claude-sonnet-4-6 \
    --explorer-model eu.anthropic.claude-haiku-4-5-20251001-v1:0 \
    --filter greptile-sentry --parallelism 1 --verbose
python ../eval/code_review/run.py --filter "requests-001" --no-judge
python ../eval/code_review/run.py --gold --gold-model sonnet     # Claude Code CLI baseline

# Full 4-suite regression harness — **runs suites sequentially**
# (not parallel) to avoid OOM-kill from 4 concurrent tree-sitter
# graphs (~12-14 GB each on sentry / grafana / keycloak).
make eval-brain-regression TAG=v2u
make eval-brain-regression TAG=fast PARALLELISM=1   # tight-RAM machines

# Agent answer quality (baseline comparison)
python ../eval/agent_quality/run_bedrock.py                  # Bedrock (Sonnet/Haiku)
python ../eval/agent_quality/run_bedrock.py --workflow --haiku  # Haiku explorer + Sonnet judge
python ../eval/agent_quality/run_bedrock.py --brain              # Brain orchestrator

# Tool parity (Python vs TS)
python ../eval/tool_parity/run.py --generate-baseline
```

## PARALLELISM guidance

`PARALLELISM` controls **case-level** concurrency within a single suite
process. It does NOT control suite-level concurrency (suites run
serially in the Makefile target).

- Default 2 — safe on any ≥16 GB machine
- Drop to 1 on tight-RAM boxes (<16 GB)
- Bump to 3+ on ≥32 GB if you want faster sentry / grafana / keycloak

Suite-level parallelism was removed in the Makefile because 4
concurrent tree-sitter graphs (sentry ~13 GB, keycloak ~14 GB,
grafana ~11 GB) overwhelm < 40 GB machines and the kernel OOM-killer
drops processes silently. Check `dmesg | grep oom_kill_process` if a
regression "vanishes" with partial data.

## Scoring

- **Code review**: `eval/code_review/run.py` — scoring: recall (35%), precision (20%), severity (15%), location (10%), recommendation (10%), context (10%)
- **Agent quality**: `eval/agent_quality/run_bedrock.py` — pattern-match answers against `required_findings` in baseline JSON
- **Tool parity**: `eval/tool_parity/run.py` — diff Python vs TS tool outputs for the same inputs
