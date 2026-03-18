---
name: explore_architecture
type: explorer
model_role: explorer
tools:
  core: true
  extra: [module_summary, list_files, get_dependencies, get_dependents, detect_patterns]
budget_weight: 1.0
input: [query, workspace_layout]
output: perspective_answer
---

## Strategy: Architecture Overview
1. Use module_summary on top-level directories to understand responsibilities
2. Use get_dependencies to map module relationships
3. Use compressed_view on key service files for interface details
4. Build a dependency diagram: Module -> depends on -> Module
Target: 5-10 iterations. Answer with architecture summary and module diagram.
IMPORTANT: Start from documentation and module_summary -- do NOT read individual files.
