---
name: explore_data_lineage
type: explorer
model_role: explorer
tools:
  core: true
  extra: [trace_variable, find_references, get_callees, get_callers, get_dependencies, ast_search]
budget_weight: 1.0
input: [query, workspace_layout]
output: perspective_answer
---

## Strategy: Data Lineage Tracing
1. Find the data source (grep the variable/field name, or find_symbol for the model)
2. Use trace_variable forward to find where the value flows
3. Chain trace_variable calls: each hop's flows_to becomes the next starting point
4. Use read_file to verify ambiguous hops (confidence="low")
5. Map the complete lineage: Source -> Transform -> Sink
Target: 8-15 iterations. Answer with complete data flow chain, citing file:line at each hop.
