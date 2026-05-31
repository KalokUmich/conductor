# Phase 14 — Score Diagnosis: how the SDK migration moved code-review scores

**Date:** 2026-05-31 · **Baselines compared:** `eval/code_review/baselines/premigration_20260529/`
(Sonnet brain + Haiku explorer, **in-house AgentLoopService** leaves) vs
`sdk_migration_20260530/` (same models, **SDK leaf workers** — Step 06c).

## Headline

The SDK migration **lifted detection and held composite roughly flat**, with **one systematic
regression (severity calibration)** and **one variance artifact (grafana)**. Per suite:

| Suite (lang) | Catch b→n | Composite b→n | Recall b→n | **Severity** b→n | **Recommend** b→n |
|---|---|---|---|---|---|
| planted requests (Py) | 100→100 | 0.923→0.923 | 1.0→1.0 | 0.625→0.625 | 1.0→1.0 |
| greptile sentry (Py) | 50→**80** | 0.758→**0.822** ↑ | 0.81→1.0 | 0.633→**0.512** ↓ | 0.517→**0.658** ↑ |
| greptile grafana (Go) | 90→80 | 0.727→**0.647** ↓ | 0.71→0.65 | 0.563→**0.430** ↓ | 0.667→**0.463** ↓ |
| greptile keycloak (Java) | 100→100 | 0.764→**0.770** ≈ | 0.85→0.88 | 0.442→**0.342** ↓ | 0.633→**0.758** ↑ |

Greptile composite avg ≈ flat (0.750→0.746). Detection (recall/catch) meets-or-beats baseline
across the board; **severity is down in all three real suites (−0.10…−0.13)** — the one consistent,
non-noise signal.

## Defect 1 — Severity under-calibration (SYSTEMATIC; the priority fix)

The SDK path **under-grades critical/security findings**. Severity is classified by the
**coordinator** (brain tier) via the rubric in `config/skills/pr_brain_coordinator.md:639-691`, which
is *guidance, not enforced*; workers emit `severity: null` + an optional `severity_hint`
(`brain.py:_compose_role_system_prompt` 195-212).

Evidence (judge prose, sentry):
- **sentry-010** (reasoning 5/5, actionability 5/5 — found & explained everything): expected
  **`critical`** → graded **`medium`**; a `warning` → graded `high`. *Analysis is perfect; only the
  severity dial is miscalibrated, and the dangerous direction is under-grading critical.*
- **sentry-004** (reasoning 3, actionability **1**): found the OAuth-verification **security bypass at
  the correct file+lines** (title/file/line/rec ✓) but **"framed it as a scope gap rather than the
  core issue"** → severity+category wrong, actionability gutted. Saw the code, mis-judged how bad it is.

Per-case severity (b→n) on sentry shows the drop concentrates on **newly-caught** bugs (002 0.83→0.33,
004 0.50→0.00 — both catch 0→1) plus modest slippage on shared ones (003 1.0→0.88, 005 0.75→0.67,
010 0.75→0.50). So the aggregate −0.12 is partly "the cost of catching more," partly real slippage.

**Scorer target** (`eval/code_review/scorer.py` `_severity_score`): exact=1.0, adjacent=0.5, weight 15%
of composite. Equivalence `warning≡medium`.

## Defect 2 — Existence-check false-positive (correctness, narrow)

**sentry-002**: planted bug is an **ImportError** (`OptimizedCursorPaginator` doesn't exist). Our
synthesis **explicitly claimed "OptimizedCursorPaginator EXISTS in paginator.py line 821 — import
works ✓"** and did NOT flag it, matching a *different* paginator bug instead (title ✓, file/line/sev ✗).
The phantom-symbol/existence reasoning (P13/P14) wrongly concluded a missing symbol exists. (Baseline
also missed 002 → not a regression, but a real correctness bug to fix.)

## Defect 3 — grafana "regression" = VARIANCE, not reasoning

grafana composite −0.08 is dominated by **one case**: **grafana-004** went `comp 0.93→0.00`
(caught→**missed**, catch 1→0). That single case ≈ the whole suite drop. The other 9 cases are
flat-or-better (005 0.93→1.0, 001 0.52→0.73, 007 0.69→0.76). The recommendation −0.20 is also mostly
004 (1.0→0.0) + a couple softer cases. **Conclusion: one missed bug (N=10 variance), not a systematic
failure.** No action beyond noting; re-runs will confirm.

## Why detection rose but severity didn't — the attribution

The **only** layer that changed for leaves is the engine (AgentLoopService → Claude Agent SDK/CLI):
- **Detection ↑** ← the SDK/CLI **harness** (production agent loop: tool-call formatting, recovery,
  compaction) makes leaf investigation more thorough → more real candidates surfaced → coordinator
  catches more (sentry recall 0.81→1.0, catch 50→80).
- **Severity flat/↓** ← severity *judgment* lives in **our** prompt, and the **Claude Code persona is
  NOT inherited** — `sdk_worker._build_options` passes `system_prompt=<str>` (full-replace of the CLI
  default, confirmed `sdk_worker.py:179`). So the harness couldn't help severity, and weak leaf framing
  (004 "scope gap") even dragged it.

**Implication:** the severity gap is **ours to fix in our prompt/skill** (sharpen the rubric; make
security-bypass=critical unmissable) and/or via a **stronger coordinator** (severity is judged at the
brain tier → the Opus 4.8 A/B directly tests this). It is *not* something adopting the Claude Code
persona would fix — and switching would dilute our role specialization (CLAUDE.md principle #8).

## Actions (feed Phase 14.2+)
1. **Severity rubric → examples** (`pr_brain_coordinator.md:639-691`), with sentry-004/010 as worked
   cases; consider a deterministic critical-pattern **severity-floor post-pass**.
2. **A/B the brain tier** (Sonnet vs Opus 4.8) on the severity-heavy sentry suite — does a stronger
   coordinator calibrate severity better, and at what $?
3. Existence-check FP (sentry-002) — tighten P13/P14 "exists ✓" confidence. (Lower priority.)
