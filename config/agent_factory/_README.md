---
name: _README
kind: factory-doc
---

# Agent Factory — role templates for v2 dynamic dispatch

This folder is **different** from `config/agents/`.

- `config/agents/*.md` — concrete v2 agent definitions that the Brain
  dispatches as-is: `pr_existence_check` (Phase 2), `pr_subagent_checks`
  (checks mode worker), `pr_verification_single` / `pr_verification_batch`
  (P11 verifiers), plus business-flow explorers. The Brain loads the
  whole file as the agent's system prompt.
- `config/agent_factory/*.md` — **reference templates** for v2's
  role-based dispatch. The PR Brain v2 coordinator **learns from** these
  templates when it decides to dispatch a role-specialized worker, then
  **composes its own system prompt** by combining:
    1. The role's Lens / Concerns / Approach / Examples (from the
       template)
    2. The PR-specific scope, direction hint, and Survey notes (from the
       coordinator's own analysis)

The factory file is a knowledge source, not a prompt to paste verbatim.
If you want to ship a new dispatchable agent, add it under
`config/agents/`. If you want to teach the coordinator a new review
lens, add it here.

## Template shape (all 4 sections required)

```markdown
---
name: <role>
description: "One-line identity for coordinator's role-picker"
model_hint: explorer | strong
tools_hint: [tool1, tool2, ...]
---

## Lens
2-3 sentences describing how this reviewer sees code. WHO they are,
WHAT perspective they bring. Keep concrete.

## Typical concerns
Bullet list of 5-10 concrete bug classes this lens catches. NOT abstract
categories ("security issues") — specific patterns ("SQL injection via
string interpolation", "session token written to log").

## Investigation approach
2-4 sentences describing HOW this reviewer typically finds their bugs.
Focus on the MOVE (trace data from X to Y; diff-before-and-after;
cross-check against recent history). This is the transferable method,
not a checklist.

## Finding-shape examples
2-3 `<example>` blocks showing what a well-written finding in this
lens looks like. Include: title, file:line, evidence (quoted code),
severity_hint, suggested fix. These examples teach by shape.
```

## Why separate from `config/agents/`

- Different composition semantics: factory = "reference / teach",
  agents = "paste as prompt".
- The factory can evolve independently — new roles, new examples, new
  investigation approaches — without touching any active agent config.
- v1's fixed `dispatch_explore("role")` path was deleted in commit
  `95f39d9`; the factory is the only place role lenses live now.

## How coordinator uses the factory

At coordinator-prompt-assembly time:
- A small "Available review roles" index is injected (just names +
  descriptions) so the coordinator knows what exists.
- When the coordinator emits `dispatch_verify(role="security", ...)`,
  the dispatch handler loads `config/agent_factory/security.md`,
  extracts the 4 sections, and composes a bespoke system prompt that
  fuses the role lens with the PR-specific context (scope, direction
  hint, Survey notes).

New roles = drop a new `.md` file into this folder + restart backend.
No code change needed.
