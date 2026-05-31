#!/usr/bin/env bash
# SDK-migration code_review eval (Step 06c gate). Apples-to-apples with
# premigration_20260529: same 4 suites, serial, Sonnet brain + Haiku explorer,
# venv python (graph tools ON). Writes to a FRESH out dir (does not clobber the
# baseline).
set -uo pipefail

cd "$(dirname "$0")/../../backend" || exit 1
PY="../.venv/bin/python"
RUN="../eval/code_review/run.py"
BRAIN_MODEL="eu.anthropic.claude-sonnet-4-6"
EXPLORER_MODEL="eu.anthropic.claude-haiku-4-5-20251001-v1:0"
PARALLELISM="${PARALLELISM:-3}"

OUT="../eval/code_review/baselines/sdk_migration_20260530"
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
  newest=$(ls -t "$BASELINES_DIR"/baseline_*.json 2>/dev/null | head -1)
  [ -n "$newest" ] && cp "$newest" "$OUT/${label}.json" && echo ">> saved $OUT/${label}.json"
}

run_suite "planted_requests" "requests"
run_suite "greptile_sentry_python" "greptile-sentry"
run_suite "greptile_grafana_go" "greptile-grafana"
run_suite "greptile_keycloak_java" "greptile-keycloak"

echo "ALL SUITES DONE  $(date -u +%FT%TZ)"
