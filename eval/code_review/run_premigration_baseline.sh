#!/usr/bin/env bash
# Pre-migration baseline (agent-SDK migration bar) — current main, Sonnet brain.
#
# Captures the "meet-or-exceed" bar before the Bedrock+Claude+SDK refactor:
#   - planted-bug suite (requests, 12 cases)  -> composite
#   - greptile real-PR suite, language subset -> catch rate
#       sentry (Python) / grafana (Go) / keycloak (Java)   [skip cal.com TS, discourse Ruby]
#
# Discipline:
#   - MUST use the project venv (../.venv) so networkx + tree-sitter graph tools are ON.
#     Bare python disables graph tools and depresses scores (false-low baseline).
#   - Suites run SERIALLY (suite-level parallel graph builds OOM-kill; case-level is safe).
#   - Brain = Sonnet, explorer = Haiku.
set -uo pipefail

cd "$(dirname "$0")/../../backend" || exit 1
PY="../.venv/bin/python"
RUN="../eval/code_review/run.py"
BRAIN_MODEL="eu.anthropic.claude-sonnet-4-6"
EXPLORER_MODEL="eu.anthropic.claude-haiku-4-5-20251001-v1:0"
PARALLELISM="${PARALLELISM:-3}"

OUT="../eval/code_review/baselines/premigration_20260529"
mkdir -p "$OUT"
BASELINES_DIR="../eval/code_review/baselines"

run_suite () {
  local label="$1" filt="$2"
  echo "============================================================"
  echo "  SUITE: $label   (filter='$filt')   $(date -u +%FT%TZ)"
  echo "============================================================"
  "$PY" "$RUN" --brain \
    --provider bedrock \
    --model "$BRAIN_MODEL" \
    --explorer-model "$EXPLORER_MODEL" \
    --filter "$filt" \
    --parallelism "$PARALLELISM" \
    --save-baseline \
    --verbose 2>&1 | tee "$OUT/${label}.log"
  # label the structured baseline JSON this run just wrote
  newest=$(ls -t "$BASELINES_DIR"/baseline_*.json 2>/dev/null | head -1)
  [ -n "$newest" ] && cp "$newest" "$OUT/${label}.json" && echo ">> saved $OUT/${label}.json"
}

run_suite "planted_requests" "requests"
run_suite "greptile_sentry_python" "greptile-sentry"
run_suite "greptile_grafana_go" "greptile-grafana"
run_suite "greptile_keycloak_java" "greptile-keycloak"

echo "ALL SUITES DONE  $(date -u +%FT%TZ)"
