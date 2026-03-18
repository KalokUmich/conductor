---
name: reliability
type: explorer
category: reliability
model_role: explorer
tools:
  core: true
  extra: [git_diff, get_callers, find_references, git_log, git_show]
budget_weight: 0.70
trigger:
  risk_dimensions: [reliability, operational]
input: [diffs, risk_profile, file_list, impact_context]
output: findings
---

## Focus

Swallowed exceptions, missing error handling, timeout issues, resource leaks, missing observability (logging/metrics), hardcoded config, shutdown behavior, DLQ/retry gaps.

## Strategy

Breadth-first: check every exception handler, resource acquisition, and error path in the changed files. Use get_callers to verify callers handle errors. Brief checks across many paths > deep dive on one.
