---
name: explore_config
type: explorer
model_role: explorer
tools:
  core: true
  extra: [find_references, trace_variable, list_files]
budget_weight: 0.8
input: [query, workspace_layout]
output: perspective_answer
---

## Strategy: Config Analysis
1. grep for the config key/setting name
2. Use find_references to find all consumers
3. Use trace_variable to understand how the config value flows
4. Use compressed_view on consumer files for context
Target: 3-6 iterations. Answer with where the config is defined, who uses it, and how.
