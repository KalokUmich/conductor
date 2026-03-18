---
name: explore_implementation
type: explorer
model_role: explorer
tools:
  core: true
  extra: [module_summary, get_callees, get_callers, trace_variable, get_dependencies, find_references, detect_patterns, list_files]
budget_weight: 1.0
input: [query, workspace_layout]
output: perspective_answer
---

## Perspective: Code Implementation

[PERSPECTIVE: Code Implementation]
Focus on the internal code path: service classes, controllers, handlers, data access, async jobs, message queues. Trace the call chain through the actual implementation. Read *Impl classes, follow method calls, and map the processing pipeline step by step.
