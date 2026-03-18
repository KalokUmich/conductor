---
name: explore_impact
type: explorer
model_role: explorer
tools:
  core: true
  extra: [find_references, get_dependents, get_dependencies, find_tests, test_outline, get_callers, detect_patterns]
budget_weight: 1.0
input: [query, workspace_layout]
output: perspective_answer
---

## Strategy: Impact Analysis
1. Find all dependents using get_dependents (who depends on this code?)
2. Use find_references to find all call sites
3. Use find_tests to identify test coverage
4. **Detect patterns**: Use detect_patterns on affected modules to identify queues, webhooks, retry logic, or transaction boundaries that amplify impact.
5. For each affected module, use compressed_view to assess severity
6. Summarize: affected modules, affected APIs, risk level, pattern risks
Target: 6-12 iterations. Answer with impact summary and risk assessment.
