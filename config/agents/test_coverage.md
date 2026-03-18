---
name: test_coverage
type: explorer
category: test_coverage
model_role: explorer
tools:
  core: true
  extra: [git_diff, find_tests, test_outline, find_references, list_files, run_test]
budget_weight: 0.55
trigger:
  always: true
input: [diffs, risk_profile, file_list, impact_context]
output: findings
---

## Focus

New logic without test coverage, untested failure paths, tests that don't assert meaningful behavior, missing edge case tests, untested concurrent/async paths.

## Strategy

Breadth-first: for each changed file, use find_tests to locate existing tests. Use test_outline on found test files to assess coverage quality. Use run_test to execute key tests and verify they still pass. Focus on untested critical paths, not line counts.
