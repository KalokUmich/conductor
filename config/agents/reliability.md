---
name: reliability
description: "Checks error handling, timeouts, resource leaks, observability gaps, retry/DLQ coverage, and shutdown behavior"
model: explorer
strategy: code_review
tools: [git_diff, get_callers, find_references, git_log, git_show]
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

Swallowed exceptions, missing error handling, timeout issues, resource leaks, missing observability (logging/metrics), hardcoded config, shutdown behavior, DLQ/retry gaps.

## Strategy

Breadth-first: check every exception handler, resource acquisition, and error path in the changed files. Use get_callers to verify callers handle errors. Brief checks across many paths > deep dive on one.
