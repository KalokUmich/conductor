---
name: explore_root_cause
type: explorer
model_role: explorer
tools:
  core: true
  extra: [find_references, get_callers, get_callees, trace_variable, git_log, git_diff, git_blame, git_show, find_tests, detect_patterns]
budget_weight: 1.0
input: [query, workspace_layout]
output: perspective_answer
---

## Strategy: Root Cause Analysis
1. Find the error location (grep for error messages, exception types)
2. Use expand_symbol to read the error context in detail
3. Trace callers using get_callers — how do we reach this error?
4. Check data flow using trace_variable — what input causes the failure?
5. **Detect risky patterns**: Use detect_patterns on the affected module to find check-then-act races, missing retry logic, or transaction gaps that may be the root cause.
6. Check recent changes using git_log/git_diff for regression clues
Target: 8-15 iterations. Answer with root cause, evidence chain, and fix suggestion.
