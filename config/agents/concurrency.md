---
name: concurrency
description: "Detects race conditions, check-then-act patterns, retry idempotency, thread safety, and deadlock potential"
model: explorer
strategy: code_review
tools: [git_diff, git_show, find_references, get_callers, get_callees, trace_variable, ast_search]
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

Check-then-act patterns, duplicate processing, token/lock lifecycle, callback replay, queue redelivery safety, retry idempotency, thread safety, deadlock potential.

## Strategy

Depth-first: identify shared-state operations, then trace each to check atomicity. Use ast_search for check-then-act patterns. Spend most tool calls proving or disproving one race at a time.
