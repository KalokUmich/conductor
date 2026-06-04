# Greptile Benchmark — Local Setup & Eval Guide

A reproduction of [Greptile's public AI Code Review Benchmark (2025)][greptile-benchmarks]
inside our own eval harness, so we can score Conductor's PR Brain against the same
50 PRs that Greptile uses to compare itself with Cursor / Copilot / CodeRabbit / Graphite.

> **Original report**: <https://www.greptile.com/benchmarks>
> Read this first — it has the full methodology, the leaderboard, and the
> per-case bug descriptions for Sentry. The other 4 repos' tables are
> JS-rendered and only visible in a real browser.
>
> **Sources of every PR in the benchmark**: the 25 fork repos under
> <https://github.com/orgs/ai-code-review-evaluation/repositories>
> (5 target repos × 5 reviewer tools — same 10 PRs in each tool fork).

---

## 1. What this benchmark tests

Greptile took **50 real-world bug-fix PRs** from 5 popular OSS projects, traced
each fix back to the commit that introduced the bug, and re-created the buggy
state on a clean fork. They then ran 5 commercial AI code reviewers against
each PR with default settings, and scored each tool on whether it left an
**explicit line-level PR comment that points to the faulty code and explains
the impact**.

That binary "did the reviewer catch the planted bug" is the **catch rate**
metric — Greptile's headline number. Their own published numbers (July 2025):

| Tool         | Overall catch rate |
|--------------|-------------------:|
| **Greptile** |              82 % |
| Cursor       |              58 % |
| Copilot      |              54 % |
| CodeRabbit   |              44 % |
| Graphite     |               6 % |

We mirror the **catch rate** metric (`scorer.py::CaseScore.catch_rate`) so we
can speak Greptile's language directly. See §6 for how to interpret it.

### Dataset breakdown

| Target repo  | Language     | Cases (auto + manual) | Sample bug types                              |
|--------------|--------------|----------------------:|-----------------------------------------------|
| sentry       | Python       |                9 + 1 | OAuth state, paginator NPE, exit-code break   |
| cal.com      | TypeScript   |                9 + 1 | async forEach pattern, dynamic import         |
| grafana      | Go / TS      |                8 + 2 | RBAC negative cache, Loki query interpolation |
| keycloak     | Java         |                9 + 1 | exit-code contract change, feature flagging   |
| discourse    | Ruby / SCSS  |                9 + 1 | float-on-flexbox layout, image downsizing     |
| **TOTAL**    |              |                **50** |                                              |

* **44 auto cases** are imported from inline `logic:` / `security:` /
  `performance:` / `syntax:` review comments left by the `greptile-apps[bot]`
  account on each PR (see `import_greptile.py`).
* **6 manual cases** are hand-annotated because Greptile's bot either
  (a) only left style nits, (b) only left a top-level summary review with
  no inline anchors, or (c) didn't review the PR at all (free-trial expired).
  See `cases/greptile_*/manual_cases.yaml` — each case is marked
  `quality: human` and the bug is identified by reading the diff against the
  base SHA.

### What we explicitly DO measure

* **catch_rate** — did PR Brain emit a finding that matches the planted bug
  on `(file, line)`? This is the headline metric, comparable to Greptile's 82 %.
* **recall** — for cases with multiple expected findings (e.g. a sentry PR
  that plants 3 bugs), what fraction did we catch?
* **precision** — what fraction of our findings match an expected one?
  ("False positives" here means findings that don't map to the planted bug —
  but they may still be real defects.)
* **severity_accuracy** — did we label the bug as critical / warning / nit
  the way Greptile did?
* **LLM judge** (`judge.py`) — qualitative assessment of detection,
  reasoning_quality, actionability, false_positive_discipline. Strict 1/3/5
  rubric, see `judge.py` for the full prompt.

### What we DON'T measure (and why)

* **Latency / cost** — out of scope. Greptile's benchmark doesn't compare these
  either, even though they matter operationally.
* **Multi-reviewer consensus** — we score one reviewer (PR Brain) at a time.
* **Style / naming nits** — explicitly excluded by the `code_review_pr` skill's
  `DO NOT FLAG` list.

---

## 2. Where the data comes from

```
                  Greptile maintains              We commit to repo            Local-only (gitignored)
                  ──────────────────              ─────────────────            ────────────────────────
greptile.com/  ┐
 benchmarks    │   ai-code-review-evaluation/         cases/greptile_<target>/    repos/<target>-greptile/
 (the         ──>  {sentry,cal.com,grafana,    ──>   ├─ cases.yaml         ──>  (git clone, ~2 GB total)
  scoreboard) │     keycloak,discourse}-greptile     ├─ manual_cases.yaml
               ┘    (5 fork repos × 10 PRs)          └─ patches/*.patch         repos/greptile_bases/
                                                                                <target>/<NNN>/
                                                                                (git archive of merge-base,
                                                                                 ~6 GB total)
```

* **The fork repos** (`ai-code-review-evaluation/{target}-greptile`) hold the
  10 test PRs as ordinary GitHub PRs. The `greptile-apps[bot]` reviewed each
  one inline; those review comments are our auto-import source of truth.
* **`cases/greptile_*/cases.yaml`** is auto-generated by `import_greptile.py`
  from the scraped review comments. Committed to the repo as the source of
  truth for "what's the bug, where, what severity".
* **`cases/greptile_*/manual_cases.yaml`** holds the 6 hand-annotated cases
  (sentry-004, discourse-005, keycloak-004, grafana-002, grafana-004,
  cal_com-002). Also committed.
* **`cases/greptile_*/patches/*.patch`** is the unified diff between the
  merge-base SHA and the head SHA, regenerated locally to apply cleanly
  against the materialized base. Committed.
* **`repos/<target>-greptile/`** are full git clones of each fork — local-only,
  gitignored. The setup script clones them on first run.
* **`repos/greptile_bases/<target>/<NNN>/`** are per-case `git archive`
  snapshots of the merge-base commit — also local-only, gitignored. The
  setup script extracts them on first run.

---

## 3. Quick start (default mode)

### Prerequisites

* `git` on PATH
* `python3` ≥ 3.10
* `~12 GB` free disk
* AWS Bedrock credentials (for the eval itself, not for setup) —
  `eu.anthropic.claude-sonnet-4-6` and `eu.anthropic.claude-haiku-4-5-20251001-v1:0`
  must be enabled in your AWS region.

### Setup (one-time, ~5 min)

```bash
make greptile-setup     # from repo root — clone forks + materialize bases
# (equivalently: cd backend && python ../eval/code_review/setup_greptile_dataset.py)
```

> Base trees got corrupted or an eval case ERRORs on `git apply`? Run
> **`make greptile-repair`** (re-extracts every base `--force` from the local
> clones; `make greptile-repair TARGET=keycloak` for one).

This is the **default mode** (Layer C only — see §5 for the layer model). It:

1. Clones the 5 fork repos anonymously to `eval/code_review/repos/<target>-greptile/`.
   These are public repos, so **no GitHub token required**.
2. For each of the 50 cases, computes `merge-base(base_sha, head_sha)` and
   extracts that snapshot via `git archive` into
   `eval/code_review/repos/greptile_bases/<target>/<NNN>/`.
3. Regenerates the patches locally as `git diff merge_base..head_sha` so they
   apply cleanly against the materialized snapshot. (See §7 for why this
   matters more than you'd think.)

You'll know it worked when you see something like:

```
Done. materialized=50 patches_regenerated=50 skipped=0 errored=0
```

### Run the eval

```bash
cd backend
python ../eval/code_review/run.py --brain \
    --provider bedrock \
    --model "eu.anthropic.claude-sonnet-4-6" \
    --explorer-model "eu.anthropic.claude-haiku-4-5-20251001-v1:0" \
    --filter greptile- \
    --verbose
```

* `--brain` routes through `PRBrainOrchestrator` v2 — the only production
  path since the legacy fleet pipeline was removed. Kept as a flag name
  for backward-compatibility with existing invocations.
* `--filter greptile-` runs only the Greptile cases (drop the flag to also
  include the 12 `requests` cases).
* `--verbose` prints per-finding match results so you can see which
  expected_findings each agent caught.
* Drop `--no-judge` to enable the LLM judge for qualitative scoring.

The eval takes **~3-4 hours** for all 50 cases (Sonnet brain + 7 Haiku
sub-agents per case + judge). Run on a smaller filter (`--filter greptile-sentry`)
first to confirm the pipeline works end-to-end before committing to the
full run.

---

## 4. Reading the report

```
Per-Case Scores:
Case                  Catch   Recall     Prec      Sev      Loc      Rec      Ctx     Comp
------------------------------------------------------------------------------------------------
greptile-sentry-001       Y    1.000    0.800    0.500    0.500    0.000    1.000    0.735
greptile-sentry-002       Y    1.000    0.667    1.000    1.000    0.667    1.000    0.823
greptile-sentry-004       .    0.000    0.000    0.000    0.000    0.000    0.000    0.000
...

Aggregate             0.940    0.876    0.762    0.500    0.762    0.595    1.000    0.751

Catch rate (Greptile-style): 47/50 = 94.0%
```

The columns:

| Column | Meaning |
|---|---|
| `Catch` | `Y` = at least one expected finding matched on file+line. `.` = missed. **This is the headline metric.** |
| `Recall` | Fraction of *all* expected findings the reviewer caught (matters for multi-bug cases). |
| `Prec` | Fraction of the reviewer's findings that match an expected one (extras count as false positives, lightly weighted). |
| `Sev` | Severity-label accuracy on matched findings. |
| `Loc` | File + line accuracy on matched findings. |
| `Rec` | Recommendation/fix-text similarity to the expected one. |
| `Ctx` | Did the reviewer touch the cross-file context the case requires? |
| `Comp` | Composite score (recall 35% / prec 20% / sev 15% / loc 10% / rec 10% / ctx 10%). |

The **Catch rate (Greptile-style)** line at the bottom is the number you
compare to Greptile's published 82 %.

### LLM judge verdicts (when enabled)

```
LLM Judge Verdicts
Case                    Compl   Reason   Action       FP      Avg
------------------------------------------------------------
greptile-sentry-001         5        4        5        4     4.55
greptile-sentry-002         5        5        5        5     5.00
greptile-sentry-004         1        1        1        1     1.00
```

The judge uses **strict 1/3/5 anchors** for `completeness` and `actionability`
(no fuzzy 2/4 — see `judge.py`'s system prompt). The four columns are:

| Column | Scale | Meaning |
|---|---|---|
| `Compl` (completeness) | 1 / 3 / 5 | Did we find the planted bug? 1 = missed, 3 = noticed but wrong line/severity/category, 5 = clean catch. |
| `Reason` (reasoning_quality) | 1–5 | Is the evidence chain verifiable from the cited code alone? 4–5 require an independently re-derivable explanation. |
| `Action` (actionability) | 1 / 3 / 5 | Is the suggested fix copy-pasteable? 1 = no fix, 3 = direction named but vague, 5 = concrete patch at the right line. |
| `FP` (false_positive_discipline) | 1–5 | Penalty for noise on secondary findings — 1 = 4+ irrelevants or any DO-NOT-FLAG violation, 5 = no extras or all extras are real bugs. |

Weighted average: completeness 40 % / reasoning 25 % / fp 20 % / action 15 %.

---

## 5. Refresh modes

You will basically never need these in normal use. Read this section only when:

* You tweak the `import_greptile.py` heuristics and want the cases.yaml to reflect them
* Greptile announces an update to their public benchmark
* Something is broken on disk

### Layer A — Re-scrape from GitHub

```bash
GITHUB_TOKEN=ghp_... python ../eval/code_review/setup_greptile_dataset.py --refresh-scrape
```

**When**: Greptile updates their fork repos. New PRs added (50 → 60), branches
force-pushed, bot reviews re-run, etc. Probably ≤ 1× per quarter.

**Cost**: ~150 GitHub API calls. Anonymous quota is 60/hour, so a token is
mandatory. A fine-grained PAT with **public_repo:read** scope is enough —
see <https://github.com/settings/personal-access-tokens>.

**Side effect**: `cases/greptile_*/cases.yaml` and `cases/greptile_*/patches/`
will show as modified in `git status`. Review the diff and commit.

### Layer B — Re-import from cached scraped JSON

```bash
python ../eval/code_review/setup_greptile_dataset.py --refresh-import
```

**When**: You changed the import logic (severity inference regex, line_range
window, category mapping, title_pattern token extraction, …) and want the
cases.yaml regenerated.

**Cost**: <1 min, no token. Reads `cases/greptile_raw/*.json` (cached scrape
output) and rewrites `cases/greptile_<target>/cases.yaml`.

**Side effect**: cases.yaml will show modified. Manual cases (in
`manual_cases.yaml`) are not touched.

### Layer C — Re-clone forks + re-materialize bases

This is the **default** mode (no flags). Run it after `git pull` brings new
case entries from a teammate, or if your local `repos/greptile_bases/` got
corrupted.

```bash
python ../eval/code_review/setup_greptile_dataset.py
python ../eval/code_review/setup_greptile_dataset.py --force         # re-extract even if present
python ../eval/code_review/setup_greptile_dataset.py --skip-clone    # assume forks already cloned
```

---

## 6. Comparing to Greptile's published numbers — caveats

Our `catch_rate` and Greptile's published catch rate use the **same definition
of "caught"** (an explicit line-level finding pointing at the planted bug),
so the headline numbers are directly comparable. But several caveats apply:

### Caveat 1 — Training-data contamination

All 5 source repos (Sentry, Cal.com, Grafana, Keycloak, Discourse) are
massively popular OSS projects, and the bug-fix commits these PRs are derived
from were public well before the model training cutoff (Sonnet 4.6: ~2025-04,
Haiku 4.5: ~2025-04). Sonnet/Haiku may **remember the original fix** for some
of these bugs and "catch" them from training memory rather than reasoning.
The same caveat applies to every commercial AI reviewer in Greptile's
leaderboard, so this is a level playing field — but **don't read our catch
rate as an absolute capability claim**, only as a relative comparison.

### Caveat 2 — Greptile's bot is the source of truth

For the 44 auto-imported cases, the `expected_findings` are derived from
**inline review comments left by `greptile-apps[bot]`** on those PRs. In other
words: the ground truth is "where Greptile's own bot said the bug was". If
Greptile's bot pointed at the wrong line or split one bug into two, our
expected_findings inherit that bias.

### Caveat 3 — The 6 manual cases are educated guesses

For sentry-004, keycloak-004, grafana-002, grafana-004, and cal_com-002, the
planted bug is **clearly identifiable** from the diff (and matches the public
Greptile severity table where available). Confidence: high.

For discourse-005, the planted bug is **a best guess** — Greptile bot only
left style nits and the public table for discourse isn't visible in the
benchmark page's HTML. Confidence: medium. The reviewer that finds the
"floats inside flexbox" anti-pattern in `topic-post.scss` will be marked as
catching.

### Caveat 4 — Sample size

50 cases is statistically thin. The 95 % CI on a true 80 % catch rate at
n=50 is roughly ±11 percentage points (`±2 √(p·(1-p)/n)`). So we can
distinguish a 60 % reviewer from a 95 % one with confidence, but **we
cannot distinguish a 75 % reviewer from an 85 % reviewer** at this sample
size. Single-case wins/losses are noise; trends across 5 repos are signal.

### Caveat 5 — Default settings only

Greptile's methodology section explicitly notes: *"all tools ran with default
settings (no custom rules or fine-tuning)."* We follow the same constraint —
PR Brain runs with the production prompt set, no per-case tuning.

### What a fair comparison looks like

| Reviewer  | Greptile-published | Our run on the same dataset |
|-----------|-------------------:|----------------------------:|
| Greptile  |               82 % |                  N/A (theirs) |
| Cursor    |               58 % |                  N/A (theirs) |
| Copilot   |               54 % |                  N/A (theirs) |
| CodeRabbit|               44 % |                  N/A (theirs) |
| Graphite  |                6 % |                  N/A (theirs) |
| **Conductor PR Brain** |    — |  **(fill in after first full eval)** |

When you want to publish a number, run the full eval **3 times** and report
the median + range. LLM nondeterminism means a single run can swing
~5–10 percentage points.

---

## 7. How the data gets onto disk — the merge-base trick

This section is for the curious — you can skip it if you just want to run
the eval. It explains why `materialize_greptile_bases.py` is more
complicated than "git checkout base_sha".

The naive approach is:

1. Download the API-returned `.diff` for each PR.
2. Materialize a snapshot of `base_sha` (the commit the PR is based on).
3. Apply the diff. ✗

This **fails** for two reasons we hit in practice:

1. **GitHub's `.diff` is computed against `merge-base(base, head)`**, not
   against `base.sha`. When the base branch advances after the PR is opened,
   `base.sha` includes commits the diff is unaware of, and `git apply` fails
   with "patch does not apply" on the unrelated changes.
2. **The base branch in these forks tends to advance** because they're
   re-published periodically to track upstream. Sentry's `master` in the
   fork has moved many SHAs ahead of where most PRs were originally opened.

The fix:

1. Resolve `head_ref` → `head_sha` against the freshly-cloned fork.
2. Compute `merge_base = git merge-base base_sha head_sha`.
3. `git archive merge_base | tar -x` → that's the snapshot.
4. `git diff merge_base head_sha` → that's the patch.

Both come from the **same** local fork clone, so they're guaranteed
consistent and the patch always applies.

This is what `materialize_greptile_bases.py::process_target` does.

---

## 8. Per-case workspace — the hardlink + atomic-write trick

§7 got **one** clean source snapshot per case onto disk. But the eval run
itself can't mutate those snapshots — every case needs a **fresh workspace**
it can `git init`, apply the patch to, and commit into, without polluting
the shared base (each snapshot is 13K-17K files, ~6 GB across all 50 cases,
and we want to reuse them run after run).

The naive solution is "for each case, `shutil.copytree` the snapshot into
a temp dir". On sentry (~17K files) this takes ~90 seconds per case. For
50 cases that is 75 minutes of pure file copying before any LLM is even
invoked — completely hostile to an interactive debug loop.

`runner.py::setup_workspace` (eval/code_review/runner.py:90-178) does it
differently: it **hardlinks** each file instead of copying, then relies on
`git apply`'s write-new-file + rename behaviour to give us per-file
copy-on-write for free.

### What setup_workspace does, step by step

```python
# 1. fresh empty temp directory
workspace = tempfile.mkdtemp(prefix="eval_ws_")

# 2. walk the snapshot; for each file, try hardlink first, fall back to copy
def _link_or_copy(s, d):
    try:
        os.link(str(s), str(d))    # new directory entry, same inode — instant
    except OSError:
        shutil.copy2(str(s), str(d))  # cross-filesystem fallback — slow but works

# 3. init a fresh git repo; the .git/ directory is workspace-specific
git init
git add -A
git commit -m "Initial: clean source"

# 4. apply the bug-introducing patch
#    --reject tolerates hunks that target excluded dirs (.github/, etc.)
git apply --reject <patch>

# 5. second commit — now HEAD~1..HEAD is exactly the "PR" under review
git add -A
git commit -m "Apply bug patch"

# 6. hand workspace + diff_spec="HEAD~1..HEAD" to PR Brain
# 7. on cleanup: shutil.rmtree(workspace) frees workspace-only inodes;
#    snapshot inodes lose one refcount but stay alive via the snapshot dir.
```

### Why hardlinks work here — the atomic-write contract

A **hardlink** is a second directory entry that points to the *same inode*
as the original file. The kernel copies no data — it just adds a row
mapping `(new path → existing inode)`. Operation cost is a few hundred
bytes of metadata, regardless of how big the file is. On sentry's 17K
files, hardlinking beats `shutil.copytree` by roughly two orders of
magnitude: ~1 second vs ~90 seconds.

The obvious worry: if `login.py` in the workspace shares an inode with
`login.py` in the snapshot, wouldn't `git apply` modifying the workspace
poison the snapshot? **No** — because `git apply` (like every
well-behaved Unix file writer) uses **write-new-file + rename**:

1. Read the old file content into memory.
2. Compute the new content.
3. Write the new content to a temp file — **this is a brand-new inode.**
4. `rename(temp, target)` — atomically repoint the workspace's directory
   entry for `target` to the temp file's new inode.

Step 4 only touches the **workspace's** directory entry. The snapshot's
directory entry for the same file still points to the **old** inode,
which is untouched. The hardlink "breaks" at exactly that one file —
every other file in the workspace stays happily hardlinked to the
snapshot.

Concretely: for a PR that changes 3 files, the workspace ends up owning
3 brand-new inodes (plus the `.git/` subdirectory). The other ~17 000
files remain shared with the snapshot. Extra disk per workspace is in
the kilobytes, not megabytes. When the workspace is `rmtree`'d after the
eval, only those workspace-only inodes are freed; the snapshot is left
in its original, unpolluted state, ready to seed the next case.

### Two filesystem rules the design depends on

1. **You cannot hardlink directories** on Linux (it would allow
   filesystem cycles and break every recursive traversal tool). That is
   why `shutil.copytree` always creates fresh directory objects for each
   directory in the workspace, and why the custom `copy_function`
   (`_link_or_copy`) only runs at the **file leaves**. Directory
   structure is real-new; only file inodes are shared.
2. **Hardlinks cannot cross filesystems.** An inode number is only unique
   within one mounted filesystem, so `os.link` fails with `EXDEV` if
   `src` and `dst` are on different mount points (e.g. `/home` on ext4
   and `/tmp` on tmpfs, or `/mnt/c` on NTFS from a WSL `/home/…` source).
   The `try os.link / except OSError: shutil.copy2` block means such
   setups degrade to "slow but correct" rather than crashing.

### What it actually saves

| Approach | Time to prepare 50 workspaces | Extra disk used |
|---|---:|---:|
| `shutil.copytree` | ~75 min | ~300 GB (50 × 6 GB) |
| hardlink + fallback | ~50 sec | a few MB total |

This is why an interactive debug loop on the full 50-case eval is
tolerable, and why the old troubleshooting entry about "60+ second
setup_workspace" no longer applies on any filesystem that supports
same-device hardlinks.

### The pattern, generalised

**Hardlink a read-only master tree, then let atomic-write tools naturally
provide per-file copy-on-write.** No special filesystem (btrfs,
overlayfs, zfs), no kernel features, no checkpoint / snapshot APIs —
plain POSIX `link(2)` plus the universal "write temp + rename"
convention is enough.

The same pattern shows up in pnpm's `node_modules` store, Nix's
`/nix/store`, `cp --link`, `rsync --link-dest`, and ccache. It is worth
remembering any time you need to hand many consumers an "independent
writable view" of a large read-only dataset — test runners, CI build
caches, container rootfs prep, eval harnesses. The cost is one
`try / except` block, and the reward is "copy effectively free, with
strict isolation".

---

## 9. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `git apply ... patch does not apply` | Base snapshot was extracted from the wrong SHA (or a hardlink-inode corruption mutated it) | `make greptile-repair` (or `make greptile-repair TARGET=<t>`) to re-extract `--force` |
| `git archive failed for ref=...` | Clone is stale (Greptile updated branches) | `rm -rf eval/code_review/repos/<target>-greptile && python setup_greptile_dataset.py` |
| `FATAL: GITHUB_TOKEN required` | Used `--refresh-scrape` without a token | Set `GITHUB_TOKEN=ghp_...` (fine-grained PAT, public_repo:read) |
| `Bedrock converse FAILED ... ExpiredTokenException` | AWS STS token expired mid-eval | Refresh credentials in `config/conductor.secrets.local.yaml` and re-run |
| `composite=0.000 (recall=0.00, findings=0)` on every case | All Bedrock calls failed | Check AWS credentials BEFORE the next run — see `~/.aws/credentials` or `conductor.secrets.local.yaml` |
| `setup_workspace` takes 60+ seconds | Hardlinks are falling back to real copy because `tmpfile.mkdtemp()` lands on a different mount point from `repos/greptile_bases/` | Set `TMPDIR` to a directory on the same filesystem as the repo, or ignore — correctness is unaffected. See §8 for why hardlinks normally make this ~1 sec. |
| Disk usage > 12 GB | You ran `--refresh-scrape` and have stale bases not cleaned up | `rm -rf eval/code_review/repos/greptile_bases && python setup_greptile_dataset.py` |

---

## 10. References

* **Greptile's benchmark report**: <https://www.greptile.com/benchmarks>
  — original methodology, leaderboard, per-case Sentry table.
* **Test PR forks**:
  <https://github.com/orgs/ai-code-review-evaluation/repositories>
  — 25 forks (5 targets × 5 reviewer tools), each with 10 open PRs.
* **Our scraper**: `eval/code_review/scrape_greptile.py`
* **Our importer**: `eval/code_review/import_greptile.py`
* **Our materializer**: `eval/code_review/materialize_greptile_bases.py`
* **Our setup wrapper**: `eval/code_review/setup_greptile_dataset.py`
* **Our scorer**: `eval/code_review/scorer.py` (computes catch_rate)
* **Our judge**: `eval/code_review/judge.py` (LLM-as-judge with strict rubric)
* **PR Brain (production code)**: `backend/app/agent_loop/pr_brain.py`
* **The 6 manual cases**: `cases/greptile_*/manual_cases.yaml`

[greptile-benchmarks]: https://www.greptile.com/benchmarks

---

## Appendix A — The git object model you need to read §7 and §8

§7 and §8 both make design decisions that only make sense if you
understand how git actually stores data under the hood. If phrases like
"blobless clone", "tree-hash pruning", or "git diff is cheap because of
content-addressed structural sharing" feel handwavy, this appendix is
for you. It is the set of notes we wish existed when first building the
pipeline.

### A.1 The three object types in `.git/objects/`

Git stores exactly three kinds of things (plus annotated tags, which do
not matter here):

| Object | Contains | Typical size |
|---|---|---|
| `commit` | metadata + one hash pointing to a `tree` + parent commit hashes | few hundred bytes |
| `tree`   | a directory listing: `[(mode, name, hash), ...]` — each entry points at a `blob` or another `tree` | hundreds of bytes to a few KB |
| `blob`   | raw file bytes — no filename, no permissions, just contents | equal to the file's actual size |

Key consequences, each of which trips up beginners:

- A `commit` **does not** contain the source code. It only carries a
  pointer to the root tree, the parent commit(s), and some metadata
  (author, date, message). Typically < 500 bytes.
- A `tree` represents **one directory only**. Subdirectories are
  separate tree objects, referenced by hash. To reconstruct the state of
  a repo at a given commit you recursively walk the DAG
  `commit → root tree → sub-trees → ... → blobs`.
- A `blob` has **no filename attached**. The filename is recorded in
  the tree entry that points to it. Two files with identical content at
  different paths share the same blob on disk — stored exactly once.
  The canonical example: thousands of empty `__init__.py` files in a
  Python monorepo, all pointing at the same blob
  `e69de29bb2d1d6434b8b29ae775ad8c2e48c5391`.
- Everything is **content-addressed**: the hash of each object is
  `SHA1(contents)`. Structurally identical trees naturally produce
  identical hashes, no coordination required.

### A.2 Snapshot-based, not delta-based

Every blob stores the **complete file content**, not a delta against
its predecessor. This is git's fundamental split from SVN/CVS. It
sounds wasteful, but two mechanisms keep the store small:

1. **Content-addressed structural sharing (logical level).** A commit
   that changes one file creates exactly:
   - 1 new blob (the new file content),
   - N new trees, where N = depth from root to the changed file (each
     such tree's child-hash changed, which changes its own hash, so each
     must be a new object),
   - 1 new commit.

   Every other tree and blob is **structurally shared** with previous
   commits — same content means same hash means same object on disk.
   A 17k-file repo with 10k commits might contain only ~50k unique
   blobs total, because most files are never touched by most commits.

2. **Packfile delta compression (physical level).** When `git gc` runs
   or during network transfer, similar blobs are grouped into packfiles
   where one is stored as a full base and others as binary deltas
   against it. This is a **pure storage optimisation** — the logical
   model remains "every blob is a full file". Users and tools see the
   uncompressed view; git transparently reconstructs blobs from deltas
   on read.

### A.3 Why `git diff A B` is so fast

`git diff merge_base head` does **not** read every file. It walks the
two tree hierarchies in parallel and prunes aggressively:

1. Take root tree `A` and root tree `B`.
2. For each entry in each side:
   - **Hashes equal** → skip the entire subtree. Hash equality means
     nothing inside it changed, because the hash is a function of the
     entire recursive contents.
   - **Hashes differ, it's a blob** → changed file. Read both blobs and
     compute a text diff.
   - **Hashes differ, it's a subtree** → recurse.
   - **One side missing the entry** → add/delete.

For a PR that changes 5 files in sentry, git diff reads a handful of
trees (those on the path from root to each changed file) and 10 blobs
(5 × 2 sides). The other 17 000 files are skipped by a single 20-byte
hash comparison each. Tree-hash pruning is git's single most underrated
optimisation.

### A.4 Why `--filter=blob:none` (blobless clone) is exactly right for us

Git partial-clone filters let you defer object downloads until they are
actually needed:

| Filter | Downloaded | Not downloaded | Good for |
|---|---|---|---|
| `--depth=1` | HEAD commit + its tree/blobs only | Any history | Build checkouts, read-only CI |
| `--filter=blob:none` | All commits + all trees | All blobs (fetched on demand) | **Our pipeline** |
| `--filter=tree:0` | All commits only | All trees + all blobs | `git log`-only workflows |

Blob-less clone is the sweet spot for us because:

- `git merge-base` needs **only** the commit-parent graph — 0 blobs.
- `git archive <merge_base>` needs every blob reachable from the
  merge-base tree (git transparently fetches them on first use — this
  is the single biggest download in our pipeline).
- `git diff <merge_base> <head>` only needs to fetch the **head-side**
  blobs for files that actually changed. The merge-base side is already
  local from the previous step, and tree-hash pruning avoids fetching
  anything for unchanged files.

Total cost of cloning one target fork: ~150 MB of commit/tree metadata
+ ~few hundred MB of blobs that are genuinely needed ≈ **~2 GB across
all 5 targets**. A full (unfiltered) clone would be 8-10 GB because it
would fetch every historical version of every file, including the >90 %
our pipeline never touches.

### A.5 Why `git archive` instead of `git checkout + cp -r`

`git archive <ref>` streams a tarball directly from the object store:

- **No working-tree mutation** — safe to run while other operations
  look at the same fork clone.
- **No `.git/` in the output** — we get a plain source tree, which is
  exactly what `setup_workspace` wants to hardlink from.
- **No dependency on which branch is checked out** — the ref is a
  parameter, not an ambient state.
- **Fast** — one tree walk plus streaming IO, no intermediate working
  tree materialisation.

`git checkout + cp -r` would force serialised access to the fork clone
(because checkout mutates HEAD), leave a `.git/` we'd have to strip,
and pay an extra full tree walk to populate the working tree before the
copy even started.

### A.6 Why blobs "never get deleted" (reachability and gc)

A question that usually pops up once the above is understood: if git
stores every historical version of every blob forever, doesn't
`.git/objects/` grow without bound?

In practice, almost yes. Git's deletion model is **reachability-based**:

1. Start from the set of refs — branches, tags, HEAD, stash, reflog.
2. Walk the graph `ref → commit → tree → subtree → ... → blob` and mark
   every reachable object.
3. Objects not marked in step 2 are eligible for deletion.
4. `git gc` runs mark-and-sweep with the above rules, but there is a
   **30-day grace period** via reflog for unreachable objects, which
   means even `git reset --hard` will not free disk until a month later.

The upshot: once you commit a file, the blob stays as long as **any**
reachable commit anywhere references it. This is a feature (version
control is supposed to be permanent), but it is also why checking a
large binary into a git repo is a famously costly mistake: undoing it
requires rewriting history (`git filter-repo`), force-pushing, and
forcing every collaborator to re-clone.

Physical size is mitigated by packfile delta compression (see A.2), but
the object *count* in `.git/objects/` only goes up in practice.

### A.7 Six facts you can carry around in your head

If you internalise just these six statements, ~80 % of git's behaviour
stops being surprising:

1. A `commit` points to a `tree`; a `tree` points to sub-trees and
   blobs; a `blob` is just the raw bytes of one file.
2. Filenames live in tree entries, not in blobs. Two files with the
   same content share one blob.
3. Git is snapshot-based, but deduplicated by content-addressing — each
   commit physically stores roughly "(path depth × 1 tree) + (1 blob)
   per changed file + 1 commit".
4. Tree-hash equality prunes entire subtrees in a diff — this is why
   `git diff` on huge repos is essentially free.
5. `--filter=blob:none` downloads every commit and tree but defers blob
   fetching to actual use — ideal when you only need a small number of
   specific commits' file contents.
6. `git archive` is a side-effect-free way to get a pure source tree at
   any commit from any clone, including bare clones and partial clones.
