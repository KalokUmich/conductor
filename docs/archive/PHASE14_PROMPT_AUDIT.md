# Phase 14 — Prompt/Skill Audit vs Anthropic Best Practices

**Date:** 2026-05-31 · **Reference:** the 17 principles in `config/CLAUDE.md` ("Agent & Prompt Design
Principles"). **Method:** recon pass over `config/skills/*.md`, `config/agents/*.md`,
`config/agent_factory/*.md`, and `backend/app/agent_loop/prompts.py`. **Confidence note:** candidate
violations below are from the recon; each is **confirmed against the file at edit time (14.3)** before
changing — do not edit on this list alone.

## The 17 principles (condensed)
1. Right altitude · 2. **Examples over rule-lists** · 3. Explain *why* · 4. **Positive framing** ·
5. Context over instructions · 6. **Three-layer language** (dial back tool-eagerness; reason over
bare commands; forceful only for safety/irreversible) · 7. Minimal tool guidance · 8. **Role
specialization** (shared strategy → Layer 3, never Layer 1 — the 60%→25% scar) · 9. Structured output
via Layer-3 strategy · 10. Arbitration in coordinator synthesis · 11. DO-NOT-FLAG list · 12. Per-agent
model tier · 13. One role sentence · 14. Goal not procedure · 15. Short (50–150w) · 16. Consult prompt
library · 17. **Validate with eval**.

## Current state (recon summary)
- **Builders** (`prompts.py`): `build_sub_agent_system_prompt` (L1 identity + L3 skills/workspace),
  `build_system_prompt` (legacy/standalone), `build_brain_prompt` (Brain meta). Skills loaded via
  `_load_skill` → `INVESTIGATION_SKILLS`. L2 tools + L4 query handled separately. **4-layer structure
  is sound** (matches principle's mandatory architecture).
- **Skills** (`config/skills/`): `pr_brain_coordinator.md`, `pr_subagent_checks.md`,
  `pr_existence_check.md`, `pr_verification_check.md`, `domain_brain_coordinator.md`.
- **Agents** (`config/agents/`, 6) + **role templates** (`config/agent_factory/`, 7): generally
  **goal-framed, short, role-distinct** — principles 13/14/15 look well-honored (low audit priority).

## Candidate findings (priority for 14.3)

| # | File / area | Principle | Candidate issue | Recommended edit |
|---|---|---|---|---|
| A | `pr_brain_coordinator.md` severity rubric (~639-691) | #2 examples-over-rules | tiers taught as **bullet rules**, not examples | rewrite as worked before/after examples (sentry-004 "scope gap", sentry-010 critical→medium). **Owned by 14.2.** |
| B | `pr_brain_coordinator.md` hard-floor / dispatch rules (~53-66) | #2 | mandatory-dispatch floors as a **rule list** | add 2–3 concrete examples beside the rule |
| C | `pr_brain_coordinator.md` (~line 20) | #4 positive framing | "you are a PLANNER and SYNTHESIZER, **not** a verifier" (negative) | reframe positively ("you plan and synthesize; verification is the workers' job") |
| D | skills broadly | #6 three-layer language | audit `CRITICAL`/`MUST`/`MANDATORY` usage — keep for safety/irreversible (valid), soften where it's mere eagerness/style | per-occurrence pass |
| E | `prompts.py` workspace context in L3 | (Phase 14.4) | workspace layout/docs injected even though the CLI does recon natively | **defer to 14.4** (drop natively-derivable ctx) |

## What is NOT a problem (do not touch)
- The 4-layer separation, role distinctness (principle #8 — the 60%→25% scar means **be conservative**:
  do not move role-specific identity into shared skills or vice-versa).
- Tool descriptions (L2) — well-written, contextual.
- Agent/role-template framing (goal-not-procedure, short).

## Hand-off
- **14.2** owns finding A (severity rubric → examples) + the severity-floor post-pass — it's the
  measured defect (see `PHASE14_SCORE_DIAGNOSIS.md`).
- **14.3** owns B/C/D after confirming each against the live file; gate on the A/B subset + agent_quality
  not regressing (principle #17 + the role-separation scar).
- **14.4** owns E (workspace-context drop) as part of the 4-layer→SDK remap.

## Resolution (2026-05-31)

- **A (severity rubric) — DONE in 14.2.** Rewritten as examples; critical now covers
  security-control removal + acceptance-criterion breaks; conservation scoped to speculative
  findings. Eval-gated: severity 0.562→0.662 (+0.10), composite 0.812→0.831, gate green (merge `08e7ef6`).
- **B/C/D — NO CHANGE WARRANTED (verified against the live file).** On re-reading
  `pr_brain_coordinator.md`:
  - **C (negative framing, L20 "PLANNER… not a verifier")** — the contrast is the point of the rule
    (Survey-tools-gather-context vs workers-verify); reframing loses clarity. Defensible.
  - **B (dispatch-floor "rule-list", 53–66)** — already carries a strong rationale (42–51) *and* a
    worked example (the plaintext-password case). Adding more would be redundant.
  - **D (CRITICAL/MUST/`non-negotiable`/`MANDATORY`)** — these guard **hard constraints** (dispatch
    floor, mandatory investigations), where forceful language is **correct** per principle #6
    (Layer-3 safety/irreversible). Not a violation.
  Changing already-compliant prompts for marginal "compliance" — validated only against high-variance
  evals (severity 0.43–0.56, composite 0.68–0.82 same-config) — would risk regression (principle #8
  scar) for ~zero gain. So 14.3 makes **no prompt edits**; the one real win (severity) shipped in 14.2.
- **E (workspace-context drop)** — deferred to 14.4 (SDK Skills remap).
