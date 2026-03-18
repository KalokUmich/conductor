---
name: correctness
type: explorer
category: correctness
model_role: explorer
tools:
  core: true
  extra: [git_diff, git_show, git_log, find_references, get_callers, get_callees, trace_variable, get_dependencies]
budget_weight: 1.0
trigger:
  risk_dimensions: [correctness]
input: [diffs, risk_profile, file_list, impact_context]
output: findings
---

## Focus

Logic errors, null/undefined access, off-by-one, race conditions, wrong conditionals, missing edge cases, breaking API contracts, state machine violations, incorrect error handling.

## Strategy

Mixed strategy: scan all diffs for suspicious patterns first, then deep-dive the top 2-3 suspects with trace_variable and get_callees. Use git_show to compare code BEFORE vs AFTER the change. Budget 3-4 tool calls for scanning, 6-8 for deep investigation.
