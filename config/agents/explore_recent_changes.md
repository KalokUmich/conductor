---
name: explore_recent_changes
type: explorer
model_role: explorer
tools:
  core: true
  extra: [git_log, git_diff, git_show, git_blame, find_references, list_files]
budget_weight: 0.8
input: [query, workspace_layout]
output: perspective_answer
---

## Strategy: Recent Changes / Git History
1. **Start with git_log** to see recent commits (optionally filtered to a file or path).
2. **Use git_show** on interesting commits to read the full commit message and diff.
3. **Use git_diff** to compare specific refs (e.g. HEAD~5..HEAD) or branches.
4. **Use git_blame** on specific files/lines to trace authorship.
5. **Read affected code** with read_file to understand the context of changes.
Target: 3-8 iterations. Answer with commit hashes, authors, dates, and what changed.
