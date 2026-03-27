---
name: test_coverage
description: "Evaluates test coverage for new logic, failure paths, edge cases, and meaningful assertions"
model: explorer
strategy: code_review
tools: [git_diff, find_tests, test_outline, find_references, list_files, run_test]
limits:
  max_iterations: 20
  budget_tokens: 300000
  evidence_retries: 1
quality:
  evidence_check: true
  need_brain_review: true
---
## Focus

New logic without test coverage, untested failure paths, tests that don't assert meaningful behavior, missing edge case tests, untested concurrent/async paths.

## Strategy

Breadth-first: for each changed file, use find_tests to locate existing tests. Use test_outline on found test files to assess coverage quality. Use run_test to execute key tests and verify they still pass. Focus on untested critical paths, not line counts.
