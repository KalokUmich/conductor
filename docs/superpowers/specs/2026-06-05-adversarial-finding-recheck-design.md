# Adversarial Finding Recheck — Design

**Date:** 2026-06-05
**Status:** approved (brainstormed with user), implemented, under test
**Author:** Claude (Conductor session)

## Problem

A first-pass PR review (`/review`, PR Brain v2) can post an **overconfident** finding
that is actually a **false positive**, and that finding can drive a negative vote.
Concrete case — **PR 14471**: the security verifier raised a 🔴 *critical* — "admin
password validation incompatible with bcrypt-encoded passwords" on
`V3CmsAuthService.java:69` (`secureEquals(request.password(), adminRole.getAdminPassword())`)
— which drove a **-5 / request_changes**. It was wrong: admin passwords are stored
as **MD5 hex** (`AdminServiceImpl#create` → `MD5Utils.getMD5Digest(...)`; v2 login
compares with plain `.equals()`), in a file the diff **never touched**. For an MD5
string `CredentialUtils.matches()` falls through to `secureEquals()`, so the
suggested fix was a no-op. A human caught it by *grepping the storage path*; the
automated review did not.

Two structural gaps explain the miss:
1. **High-confidence findings skip re-verification.** The existing P11 precision
   filter only re-verifies *medium*-confidence (0.5–0.8) findings; high-confidence
   ones are kept unverified — exactly where overconfident false positives hide.
2. **The existing verifier is diff-only.** P11 uses a zero-tool `fork_call` that
   reasons over the diff text. The decisive evidence (MD5 storage) lived in a file
   *outside* the diff, unreachable without tools.

## Goal

An **adversarial second pass** that runs after a review, re-examines the
**vote-driving** findings (critical / high) with a **tool-using Opus judge** that
**must grep the actual code** (storage / write / definition sites, not just the
diff) to **refute** a finding, and — only with concrete code evidence — **resolves**
(closes, with an evidence note) the false-positive's comment thread.

**It never changes the vote.** (User decision: unverified findings may still be
real — e.g. PR 14471's *high* timing finding is genuine — so approval stays a human
call. The pass only retracts proven false positives.)

Secondary: every verdict + evidence is logged (JSONL) as future training data to
tune the first-pass reviewer. Building that loop is out of scope (YAGNI).

## Non-goals
- Not a re-review of the diff (that's `/recheck`). It only audits already-posted findings.
- Not auto-approval / vote changes of any kind.
- Not the PR-design-optimization loop (just log the data for later).

## Approach (chosen: A — standalone post-review pass)

- **A (chosen):** standalone module + endpoint, run after `/review`. Demoable in
  isolation; keeps Opus cost out of the hot review path in v1; easy to A/B and eval.
- B (rejected for v1): fold into P11 inline — bloats a tuned hot path, hard to demo.
- C (variant of A): same as A's endpoint. v2 may also hook `on_synthesize_complete`.

## Architecture

Engine-agnostic core (`backend/app/integrations/azure_devops/adversarial_recheck.py`)
that takes a pluggable `judge: async (PostedFinding) -> AdversarialVerdict`. Mirrors
`recheck.py`: **parse threads → judge → act → report**.

```
/adversarial-recheck {project, repo, pr_id, apply, judge_resolved, concurrency}
  1. client.get_pull_request → source/target branch → diff_spec
  2. client.list_threads → parse_review_threads (reused)         [recheck.py]
  3. extract_findings: badge → severity; keep critical/high;     [new]
       skip already-resolved unless judge_resolved
  4. create_pr_worktree(main_workspace, source_branch, pr_id)    [workspace.py]
  5. per finding (bounded concurrency): judge(finding)
       SDK/Opus judge: SdkWorkerRunner(model=opus, read-only MCP tools,
       CachedToolExecutor over the worktree) + adversarial system prompt
  6. decide: refuted AND has_evidence ⇒ actionable               [guardrail]
  7. apply: client.reply_to_thread(evidence correction) +
            client.update_thread_status(closed)   — NEVER vote()
  8. log every verdict to ~/.conductor/adversarial_recheck/<task>.jsonl
  9. cleanup worktree; return before/after report
```

### Components & boundaries
- `parse_severity` / `parse_title` — reconstruct severity from the posted badge
  (🔴 Critical, ⚠️/🟠 Issue/Warning→high, 🟢 Nice→praise, 🔵 Suggestion→nit). Pure.
- `extract_findings(priors, severities=VOTE_DRIVING, include_resolved)` — pure filter.
- `AdversarialVerdict.is_actionable_refutation` — **the guardrail**: `verdict ==
  "refuted" and has_evidence` (≥1 evidence item with a real `file`). Pure.
- `make_sdk_judge` — production engine (`SdkWorkerRunner`, Opus, container).
- `make_inhouse_judge` — host demo engine (`AgentLoopService`, Opus) for fast
  iteration without a container rebuild. Same prompt + tools; only the loop differs.
- `run_adversarial_recheck` — orchestration; resolves only actionable refutations in
  apply mode; logs all; returns report. `format_report` — console/endpoint summary.
- Judge prompt: `config/agents/pr_adversarial_recheck.md` (bind-mounted, tunable
  without a rebuild) + embedded fallback.
- Endpoint: `POST /api/integrations/azure-devops/adversarial-recheck` (router.py).
- Demo: `backend/scripts/adversarial_recheck_demo.py` (host venv, in-house judge).

### Judge contract (STRICT JSON)
```json
{"verdict":"holds|refuted|downgrade","new_severity":"high|medium|low|nit|null",
 "evidence":[{"file":"path","line":1,"snippet":"line you read"}],
 "reason":"one sentence citing the grepped code"}
```
- `refuted` allowed **only** with ≥1 real evidence item; default `holds`.
- Refutation without evidence ⇒ ignored, finding kept (logged as guardrail save).

## Error handling / safety
- Judge/SDK failure ⇒ verdict `holds` (fail safe — never drop a finding on error).
- Apply failure on one thread ⇒ logged per-thread, others proceed.
- Vote is never touched by any code path.
- Worktree always cleaned up (finally).
- All heavy imports (claude_agent_sdk, AgentLoopService) are lazy so the module +
  unit tests import on the host without the SDK.

## Testing
- Unit (`tests/test_adversarial_recheck.py`, 20 tests, no network): severity parse,
  vote-driving filter, resolved-skip, **evidence guardrail** (refuted-no-evidence ⇒
  kept), JSON extraction, orchestration (apply resolves only refuted+evidence;
  dry-run resolves nothing; vote never touched).
- Live demo: `--pr 14471` (in-house Opus) must auto-refute the MD5 critical with
  grepped evidence and HOLD the real timing finding.
- Final: `/adversarial-recheck` endpoint (SDK/Opus) on PR 14476.

## Rollout
v1: manual trigger (endpoint / demo). v2 (future): hook `on_synthesize_complete` to
auto-fire after each review; consider folding into P11 so corrections happen before
the first post (no retract needed).
```
