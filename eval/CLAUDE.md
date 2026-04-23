# Eval CLAUDE.md

Three eval suites. See `eval/README.md` for full docs.

```
eval/
├── code_review/        12 requests + 10 sentry + 10 grafana + 10 keycloak cases
│                       (Greptile-style composite scorer + LLM Judge)
├── agent_quality/      Agentic loop answer quality vs baselines
└── tool_parity/        Python vs TS tool output comparison
```

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
python ../eval/agent_quality/run_qwen.py --workflow            # Qwen (DashScope)

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
- **Agent quality**: `eval/agent_quality/run_bedrock.py` / `run_qwen.py` — pattern-match answers against `required_findings` in baseline JSON
- **Tool parity**: `eval/tool_parity/run.py` — diff Python vs TS tool outputs for the same inputs
