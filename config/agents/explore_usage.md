---
name: explore_usage
type: explorer
model_role: explorer
tools:
  core: true
  extra: [find_tests, test_outline, list_files, find_references, get_dependencies]
budget_weight: 1.0
input: [query, workspace_layout]
output: perspective_answer
---

## Perspective: Tests & External Interfaces

[PERSPECTIVE: Tests & External Interfaces]
Focus on how this feature looks from the outside: E2E tests (Playwright, Cypress, Selenium specs), integration tests, API specs, frontend components, page routes, step wizards, and documentation. Tests describe the actual user-visible behavior and the end-to-end journey in order. Start by searching for test/spec files related to the topic.
