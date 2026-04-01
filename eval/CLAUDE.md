# Eval CLAUDE.md

Three eval suites. See `eval/README.md` for full docs.

```
eval/
├── code_review/        12 planted-bug cases against requests v2.31.0
├── agent_quality/      Agentic loop answer quality vs baselines
└── tool_parity/        Python vs TS tool output comparison
```

## Commands

```bash
cd backend

# Code review quality (12 planted-bug cases)
python ../eval/code_review/run.py --provider anthropic --model claude-sonnet-4-20250514
python ../eval/code_review/run.py --filter "requests-001" --no-judge
python ../eval/code_review/run.py --brain --no-judge --verbose   # PR Brain mode
python ../eval/code_review/run.py --gold --gold-model sonnet     # Claude Code CLI baseline

# Agent answer quality (baseline comparison)
python ../eval/agent_quality/run_bedrock.py                  # Bedrock (Sonnet/Haiku)
python ../eval/agent_quality/run_bedrock.py --workflow --haiku  # Haiku explorer + Sonnet judge
python ../eval/agent_quality/run_bedrock.py --brain              # Brain orchestrator
python ../eval/agent_quality/run_qwen.py --workflow            # Qwen (DashScope)

# Tool parity (Python vs TS)
python ../eval/tool_parity/run.py --generate-baseline
```

## Scoring

- **Code review**: `eval/code_review/run.py` — scoring: recall (35%), precision (20%), severity (15%), location (10%), recommendation (10%), context (10%)
- **Agent quality**: `eval/agent_quality/run_bedrock.py` / `run_qwen.py` — pattern-match answers against `required_findings` in baseline JSON
- **Tool parity**: `eval/tool_parity/run.py` — diff Python vs TS tool outputs for the same inputs
