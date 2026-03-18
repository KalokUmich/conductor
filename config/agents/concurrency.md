---
name: concurrency
type: explorer
category: concurrency
model_role: explorer
tools:
  core: true
  extra: [git_diff, git_show, find_references, get_callers, get_callees, trace_variable, ast_search]
budget_weight: 0.85
trigger:
  risk_dimensions: [concurrency]
input: [diffs, risk_profile, file_list, impact_context]
output: findings
---

## Focus

Check-then-act patterns, duplicate processing, token/lock lifecycle, callback replay, queue redelivery safety, retry idempotency, thread safety, deadlock potential.

## Strategy

Depth-first: identify shared-state operations, then trace each to check atomicity. Use ast_search for check-then-act patterns. Spend most tool calls proving or disproving one race at a time.
