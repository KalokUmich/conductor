#!/usr/bin/env bash
# Guarded runner — wall-clock timeout + auth/throttle/stall detection so a Bedrock
# (or any long) command can NEVER silently hang for hours.
#
# Why: a Phase-14 eval run hung ~2h on an expired SSO token (boto3 retries + the SDK
# CLI subprocess wait on ExpiredToken with no wall-clock cap). This wrapper bounds
# every run and fails fast on the signatures that mean "dead, not working".
#
# Usage:  scripts/guarded_run.sh <timeout_s> <logfile> -- <cmd...>
#   e.g.  scripts/guarded_run.sh 1500 /tmp/ab.log -- ../.venv/bin/python ../eval/code_review/run.py --brain ...
#
# Exit codes: 0 = cmd succeeded · <cmd rc> on cmd failure · 124 = timeout ·
#             70 = auth failure · 71 = throttle · 72 = stalled (no output)
#
# Env: STALL_MIN (default 10) minutes of no log growth → kill;
#      POLL (default 20) seconds between checks;
#      SKIP_CRED_GATE=1 to skip the pre-flight cred check.
set -uo pipefail

TIMEOUT="${1:?usage: guarded_run.sh <timeout_s> <logfile> -- <cmd...>}"; shift
LOG="${1:?usage: guarded_run.sh <timeout_s> <logfile> -- <cmd...>}"; shift
[ "${1:-}" = "--" ] && shift
[ "$#" -ge 1 ] || { echo "GUARD: no command given" >&2; exit 64; }

STALL_MIN="${STALL_MIN:-10}"
POLL="${POLL:-20}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

: > "$LOG"

# Pre-flight cred gate (fail fast before spending wall-clock).
if [ "${SKIP_CRED_GATE:-0}" != "1" ] && [ -x "$REPO_ROOT/scripts/refactor/check_creds.sh" ]; then
  if ! bash "$REPO_ROOT/scripts/refactor/check_creds.sh" >/dev/null 2>&1; then
    echo "GUARD: cred-gate FAILED before launch — aborting (refresh creds)." | tee -a "$LOG"
    exit 70
  fi
fi

echo "GUARD: launch timeout=${TIMEOUT}s stall=${STALL_MIN}m poll=${POLL}s :: $*" | tee -a "$LOG"
timeout "$TIMEOUT" "$@" >> "$LOG" 2>&1 &
PID=$!

status="done"
last_size=0
stall=0
AUTH_RE='ExpiredToken|UnrecognizedClient|InvalidSignatureException|The security token included in the request is (expired|invalid)'
THROTTLE_RE='ThrottlingException|TooManyRequestsException|Rate exceeded'

while kill -0 "$PID" 2>/dev/null; do
  sleep "$POLL"
  if grep -qiE "$AUTH_RE" "$LOG" 2>/dev/null; then
    echo "GUARD: AUTH failure in log — killing (token likely expired)." | tee -a "$LOG"
    kill "$PID" 2>/dev/null; status="auth"; break
  fi
  if grep -qiE "$THROTTLE_RE" "$LOG" 2>/dev/null; then
    echo "GUARD: THROTTLE in log — killing." | tee -a "$LOG"
    kill "$PID" 2>/dev/null; status="throttle"; break
  fi
  sz=$(wc -c < "$LOG" 2>/dev/null || echo 0)
  if [ "$sz" -le "$last_size" ]; then
    stall=$((stall + POLL))
    if [ "$stall" -ge $((STALL_MIN * 60)) ]; then
      echo "GUARD: STALLED ${STALL_MIN}m (no log growth) — killing." | tee -a "$LOG"
      kill "$PID" 2>/dev/null; status="stalled"; break
    fi
  else
    stall=0; last_size="$sz"
  fi
done

wait "$PID" 2>/dev/null; rc=$?

case "$status" in
  done)
    if [ "$rc" = "124" ]; then echo "GUARD: RESULT=timeout (${TIMEOUT}s cap)"; exit 124; fi
    echo "GUARD: RESULT=done rc=$rc"; final=$rc ;;
  auth)     echo "GUARD: RESULT=auth";     final=70 ;;
  throttle) echo "GUARD: RESULT=throttle"; final=71 ;;
  stalled)  echo "GUARD: RESULT=stalled";  final=72 ;;
esac
echo "GUARD: --- last 8 log lines ---"
tail -8 "$LOG" 2>/dev/null | sed 's/[^[:print:]]//g' | cut -c1-160
exit "${final:-0}"
