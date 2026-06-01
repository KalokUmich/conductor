#!/usr/bin/env bash
# Phase 14 A/B harness: run the representative severity subset through code_review
# with a given BRAIN model, saving per-case baseline JSON + a combined log (so
# ab_report.py can attribute token usage + cost per case per model).
#
# Usage: run_ab_severity.sh <brain_model_id> <label>
#   e.g. run_ab_severity.sh eu.anthropic.claude-sonnet-4-6 sonnet
#        run_ab_severity.sh eu.anthropic.claude-opus-4-8   opus48
# Explorer is always Haiku 4.5. Suites run serially; --parallelism 1 (memory-safe).
set -uo pipefail

cd "$(dirname "$0")/../../backend" || exit 1
PY="../.venv/bin/python"
RUN="../eval/code_review/run.py"
BRAIN="${1:?usage: run_ab_severity.sh <brain_model_id> <label>}"
LABEL="${2:?usage: run_ab_severity.sh <brain_model_id> <label>}"
EXPLORER="eu.anthropic.claude-haiku-4-5-20251001-v1:0"

OUT="../eval/code_review/baselines/ab_${LABEL}"
BASELINES="../eval/code_review/baselines"
mkdir -p "$OUT"

# Representative subset: severity-weakness cases + variance/control + composite controls.
CASES=(
  greptile-sentry-002 greptile-sentry-004 greptile-sentry-010
  greptile-grafana-004 greptile-grafana-009
  requests-001 greptile-keycloak-001
)

: > "$OUT/run.log"
echo "AB START label=$LABEL brain=$BRAIN $(date -u +%FT%TZ)" | tee -a "$OUT/run.log"
for c in "${CASES[@]}"; do
  echo "===CASE $c ($LABEL)===" | tee -a "$OUT/run.log"
  "$PY" "$RUN" --brain --provider bedrock --model "$BRAIN" --explorer-model "$EXPLORER" \
    --filter "$c" --parallelism 1 --save-baseline --verbose 2>&1 | tee -a "$OUT/run.log"
  newest=$(ls -t "$BASELINES"/baseline_*.json 2>/dev/null | head -1)
  [ -n "$newest" ] && cp "$newest" "$OUT/${c}.json"
done
echo "AB DONE label=$LABEL $(date -u +%FT%TZ)" | tee -a "$OUT/run.log"
