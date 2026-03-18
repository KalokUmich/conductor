---
name: explore_entry_point
type: explorer
model_role: explorer
tools:
  core: true
  extra: [find_references, get_callees, list_files]
budget_weight: 0.8
input: [query, workspace_layout]
output: perspective_answer
---

## Strategy: Entry Point Discovery
1. grep for route/endpoint patterns matching the query terms
2. Use find_symbol to locate handler functions
3. Use compressed_view on the handler file to understand structure
4. Trace inward using get_callees if the handler delegates
Target: 3-6 iterations. Answer with the entry point file, function, and line number.
