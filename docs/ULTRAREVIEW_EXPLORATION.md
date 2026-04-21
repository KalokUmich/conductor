# Exploring Claude Code `/ultrareview` — protocol for comparing against our v2 PR Brain

## What we're trying to learn

1. **Does UltraReview catch bugs we miss?** If so, on what class of bugs?
2. **Does it miss bugs we catch?** Role-diverse specialists may win where a generic "fleet" loses
3. **What does its review output structure look like?** We can borrow good UX patterns for our synthesis
4. **What's its false-positive rate** on cases where Greptile's ground truth disagrees (e.g. sentry-001's phantom `OptimizedCursorPaginator` that actually DOES exist in the patched tree)
5. **What pace / cost does it run at?** 5-10 min at $5-20 is the advertised envelope; we confirm

We don't have access to its intermediate thinking process — it runs in Anthropic's cloud sandbox, output is the final findings list as a chat notification. That's the lens we compare.

## Why we can't observe internals directly

- `/ultrareview` is a Claude Code **UI-only slash command**. I tried invoking it via the Skill tool earlier and got back `"ultrareview is a UI command, not a skill. Ask the user to run /ultrareview themselves — it cannot be invoked via the Skill tool."`
- It dispatches to remote Anthropic-owned sandboxes. Intermediate agent dispatches, tool calls, and reasoning are not surfaced.
- The only thing we CAN introspect live (via `/tasks` in your session) is progress + partial status + final output.

So the methodology is **black-box comparison**: give it the same input we gave our Brain, compare the output.

## Fair-comparison protocol

### Step 1 — Pick 3 representative cases

From our eval suite, 3 cases that span the bug-class distribution:

| Case | Repo / PR | Why picked |
|---|---|---|
| sentry-001 | `ai-code-review-evaluation/sentry-greptile#1` | Phantom-import pattern (our P13 target). Greptile ground truth is stale (the symbol DOES exist in patch). Tests whether UltraReview reproduces Greptile's FP or sees through it. |
| grafana-009 | `ai-code-review-evaluation/grafana-greptile#9` | Multi-site stub bug (our P14 target). Tests whether UltraReview's "fleet" catches all call sites or just the stub definition. |
| keycloak-003 | `ai-code-review-evaluation/keycloak-greptile#3` | Java, 4 parallel `UnsupportedOperationException` stubs. First Java case we've measured. Tests both P14.java and UltraReview's Java depth. |

### Step 2 — Run each one

**Important**: `/ultrareview <N>` requires the `cwd` to be a git repo with a `github.com` remote pointing at the target fork. Our greptile fork clones already satisfy this.

```bash
# Sentry case — from YOUR prompt (I can't invoke /ultrareview)
cd /home/kalok/conductor/eval/code_review/repos/sentry-greptile
/ultrareview 1

# Grafana case
cd /home/kalok/conductor/eval/code_review/repos/grafana-greptile
/ultrareview 9

# Keycloak case
cd /home/kalok/conductor/eval/code_review/repos/keycloak-greptile
/ultrareview 3
```

**Budget**: Pro/Max plans include 3 free runs total (one-time). After that, $5-20 per run as extra-usage. So these 3 cases consume the entire free allotment — pick wisely. If extra-usage isn't enabled on your account, Claude Code will block the launch and link you to settings.

**Confirmation dialog**: before each run, Claude Code shows scope (file + line count when reviewing a branch), remaining free runs, and estimated cost. Confirm to start.

### Step 3 — Tracking a running review

While UltraReview runs (5-10 min each, up to 30 min for all 3):

- `/tasks` in your session to see the running list
- Open a task's detail view (also via `/tasks`) to see progress — **this is as close as we get to observing its thinking**
- You can keep working in other terminals / chats; the review runs in background
- **Do NOT close the session** — when the session closes, partial results are lost
- Completion notification arrives in the Claude Code chat

What to capture from the detail view if possible:

- Time-series of progress messages (any that mention sub-agent dispatch, verification passes, etc.)
- Any "Reviewing X / N" counters
- If the UI surfaces any breakdown by review lens (security / correctness / …), screenshot it

### Step 4 — Data to send me for each case

For each completed review, paste me the full structured output. Ideally as:

```
=== UltraReview result: sentry-greptile#1 ===
Duration: 6m 42s
Status: completed

Findings (verbatim, all):
1. [CRITICAL] src/sentry/api/endpoints/organization_auditlogs.py:11
   "ImportError at runtime: OptimizedCursorPaginator not defined in
    sentry.api.paginator"
   — {full quoted explanation}

2. [WARNING] src/sentry/api/endpoints/organization_auditlogs.py:82
   "paginate() does not accept enable_advanced_features kwarg"
   — {full quoted explanation}

...

Any progress messages you noticed mid-run:
- "Planning investigation..."
- "Dispatched 4 reviewers in parallel"
- ...

Anything surprising about UX / format:
- Grouped by file / by severity / by reviewer?
- Code snippets inline?
- Suggested fixes?
```

I'll produce a side-by-side comparison in `docs/ULTRAREVIEW_COMPARISON.md` for each case.

### Step 5 — Side-by-side metrics I'll compute

For each of the 3 cases, our v2n vs UltraReview:

| | Our v2n | UltraReview |
|---|---|---|
| Findings count | — | — |
| Catch rate (matched expected on file:line) | — | — |
| False-positive rate (findings scorer marked extra) | — | — |
| Critical findings count | — | — |
| Did it see through sentry-001 ground-truth FP? | ? | ? |
| Did it find both grafana-009 stub callers? | ✓ (P14) | ? |
| Did it find all 4 keycloak-003 stubs? | ✓ (P14.java) | ? |
| Cost | ~$0.10 each on Bedrock | $5-20 each |
| Wall-clock | ~8 min (Phase 2 timeout dominated) | 5-10 min claimed |

## What we borrow if UltraReview wins somewhere

- If UltraReview outputs **per-reviewer-lens attribution** → we adopt that UX in our synthesis (our P12 role-dispatch metadata is already in the code; just need synthesis to render it)
- If UltraReview's "independently reproduced" phrasing corresponds to a visible verification pass → we strengthen our P11 (per-finding verifier) from cheap to full LLM verification
- If UltraReview catches a bug class we systematically miss → we add a matching mechanical detector (P13/P14-style) or a new role template to `config/agent_factory/`

## What we won't do

- **Reverse-engineer their pricing model** to match them. Our cost envelope is 50× lower on purpose (every-PR assistant vs pre-merge heavyweight).
- **Copy their prompt wholesale** even if we could see it. The prompt is a symptom of an architecture; we copy architectural principles (indepenent verification, role diversity) not literal wording.
- **Run all 10+ cases of each suite** through UltraReview. Budget-infeasible; diminishing returns vs the 3 representative cases.

## If you only have time for 1 case

Run `/ultrareview 9` in `grafana-greptile`. It's the richest comparison because:
1. It's our P14 target — we know exactly what we catch
2. It's a multi-site bug — tests UltraReview's "fleet" claim
3. It's Go — different language than our strongest case (Python)

Send me the full output + any progress notes.
