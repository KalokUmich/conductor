---
name: correctness
description: "Finds logic errors, null access, off-by-one, wrong conditionals, missing edge cases, and state machine violations"
model: explorer
strategy: code_review
tools: [git_diff, git_show, git_log, find_references, get_callers, get_callees, trace_variable, get_dependencies]
limits:
  max_iterations: 20
  budget_tokens: 300000
  evidence_retries: 1
quality:
  evidence_check: true
  min_file_refs: 2
  min_tool_calls: 3
  need_brain_review: true
---
## Focus

Logic errors, null/undefined access, off-by-one, race conditions, wrong conditionals, missing edge cases, breaking API contracts, state machine violations, incorrect error handling.

## Strategy

Mixed strategy: scan all diffs for suspicious patterns first, then deep-dive the top 2-3 suspects with trace_variable and get_callees. Use git_show to compare code BEFORE vs AFTER the change. Budget 3-4 tool calls for scanning, 6-8 for deep investigation.
