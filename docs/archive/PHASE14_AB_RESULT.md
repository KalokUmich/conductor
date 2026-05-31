# Phase 14 — A/B: Sonnet 4.6 vs Opus 4.8 (brain/coordinator), greptile-sentry

**Date:** 2026-05-31 · **Setup:** PR-Brain coordinator = Sonnet 4.6 vs Opus 4.8; explorer (leaf) =
Haiku 4.5 in both; full `greptile-sentry` suite (10 cases); run via `guarded_run.sh` on SSO
auto-refresh (auth held the entire ~44 min Sonnet / ~58 min Opus run, `auth_err=0`).

## Result

| metric | Sonnet 4.6 | Opus 4.8 | Δ (Opus−Sonnet) |
|---|---|---|---|
| catch rate | 0.600 (6/10) | 0.600 (6/10) | 0.000 |
| recall | 0.967 | 0.633 | **−0.334** |
| precision | 0.887 | 0.873 | −0.014 |
| **severity** | **0.562** | **0.400** | **−0.162** |
| location | 0.658 | 0.650 | −0.008 |
| recommendation | 0.458 | 0.550 | +0.092 |
| **composite** | **0.812** | **0.676** | **−0.136** |
| **cost (10 cases)** | **$4.91** | **$10.72** | **+$5.81 (≈2.2×)** |

Cost = coordinator (brain) tokens × list price (Opus 4.8 $5/$25/$0.50/$6.25 in/out/cache-rd/cache-wr;
Sonnet $3/$15/$0.30/$3.75). Leaf (Haiku) cost is ~$0.03 in both and **under-counted** (the SDK
`ResultMessage.usage` doesn't surface full leaf consumption) — but leaves are identical Haiku in both
arms, so the cost *delta* is entirely the coordinator.

## Verdict: do NOT switch the default brain to Opus 4.8 (on this evidence)

- **Opus 4.8 scored lower** on composite (−0.136), severity (−0.162), and recall (−0.334), tied on
  catch, and only edged recommendation (+0.092) — at **2.2× the cost**. No quality justification for the
  premium here.
- **The severity hypothesis is refuted.** A stronger coordinator did *not* fix the severity
  under-grading (it got *worse*, 0.40 vs 0.56). This **confirms `PHASE14_SCORE_DIAGNOSIS.md`**: severity
  is decided by **our prompt/rubric** (unchanged across the A/B), so the lever is **14.2 (rewrite the
  severity rubric as examples + critical-pattern floor pass), not the model.**

## Caveats (read before treating as final)
- **Single run, N=10, high run-to-run variance.** Sonnet sentry composite across recent runs: 0.758 →
  0.822 → 0.812; severity 0.43 → 0.51 → 0.56. Opus's 0.676 / sev 0.40 is below all Sonnet draws, and the
  recall gap (0.63 vs 0.97 — Opus flagged fewer expected findings) is the main driver and the noisiest
  dimension. A definitive model verdict would need 2–3 repeats per arm (~$30+, ~3h) — **not worth it**
  given Opus is clearly not *better* here and costs 2.2×.
- Cross-language untested (this is Python/sentry only). If a model decision ever matters, the 7-case
  mixed subset (sentry/grafana/keycloak/requests) would check Go/Java too.

## Recommendation
1. **Keep Sonnet 4.6 as the default brain.** Do not flip to Opus 4.8.
2. **Proceed to 14.2** — fix severity in the **rubric** (`config/skills/pr_brain_coordinator.md`) +
   a deterministic critical-pattern floor pass. That's where the measured defect actually lives.
3. Opus 4.8 stays *registered* (available for ad-hoc hard cases), just not the default.

*(Production default brain is NOT being changed by this doc — recommendation only, per the plan.)*
