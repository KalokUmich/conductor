# Code Review Eval System

Integration test / eval system for `CodeReviewService` that measures review quality against known bugs planted in real open-source repos.

## Architecture

```
eval/
├── run.py              # CLI entrypoint
├── runner.py           # Workspace setup + CodeReviewService execution
├── scorer.py           # Deterministic scoring (recall, precision, etc.)
├── judge.py            # LLM-as-Judge qualitative evaluation
├── report.py           # Report generation + baseline comparison
├── repos.yaml          # Repo manifest
├── repos/              # Plain source trees (no .git)
│   └── requests/       # requests v2.31.0 source
├── cases/
│   └── requests/
│       ├── cases.yaml  # 12 case definitions with ground truth
│       └── patches/    # 12 .patch files
└── baselines/          # Timestamped JSON baselines for regression detection
```

## How It Works

1. **Runner** copies a clean source tree to a temp dir, initializes git, applies a patch (planting a bug), and commits
2. **CodeReviewService.review()** runs against `HEAD~1..HEAD` — the full multi-agent pipeline
3. **Scorer** matches findings against ground truth using regex pattern matching on title, file, line range, severity, and category
4. **Judge** (optional) uses an LLM to qualitatively evaluate completeness, reasoning, actionability, and false positive quality
5. **Report** compares against the latest baseline and flags regressions (>10% composite drop)

## CLI Usage

```bash
# Run all cases
python eval/run.py --provider anthropic --model claude-sonnet-4-20250514

# Run a single case
python eval/run.py --filter "requests-001"

# Deterministic scoring only (no LLM judge cost)
python eval/run.py --no-judge

# Save results as baseline for future regression detection
python eval/run.py --save-baseline

# Use Bedrock provider
python eval/run.py --provider bedrock --model us.anthropic.claude-sonnet-4-5-20250929-v1:0

# Use a lighter model for sub-agents
python eval/run.py --provider anthropic --model claude-sonnet-4-20250514 --explorer-model claude-haiku-4-5-20251001

# Run 3 cases in parallel
python eval/run.py --parallelism 3
```

## Scoring Rubric

### Deterministic Scores (scorer.py)

| Dimension | Weight | What It Measures |
|-----------|--------|-----------------|
| Recall | 35% | Fraction of planted bugs found |
| Precision | 20% | Fraction of findings that are true positives |
| Severity Accuracy | 15% | Correct severity assignment |
| Location Accuracy | 10% | Correct file + line range |
| Recommendation | 10% | Suggested fix matches expected |
| Context Depth | 10% | Cross-file exploration completed |

**Composite** = weighted sum of all dimensions (0.0–1.0).

### LLM Judge Scores (judge.py)

4 criteria, each scored 1–5:

| Criterion | What It Measures |
|-----------|-----------------|
| Completeness | Did the review find all planted bugs? |
| Reasoning Quality | Is the analysis well-reasoned with evidence? |
| Actionability | Are suggestions concrete and fixable? |
| False Positive Quality | Are non-bug findings legitimate? |

## Adding a New Repo

1. Clone the repo at a specific version and remove `.git`:
   ```bash
   git clone --depth 1 --branch v1.0.0 https://github.com/org/repo.git eval/repos/repo
   rm -rf eval/repos/repo/.git
   ```

2. Add to `eval/repos.yaml`:
   ```yaml
   repos:
     repo:
       source_dir: repos/repo
       version: "1.0.0"
       language: python
   ```

3. Create `eval/cases/repo/cases.yaml` and `eval/cases/repo/patches/`

## Adding a New Case

1. Create a patch against the source tree:
   ```bash
   cd eval/repos/requests
   # Make your buggy change
   git diff > ../../cases/requests/patches/NNN-description.patch
   ```

2. Add the case definition to `cases.yaml`:
   ```yaml
   - id: requests-NNN
     patch: patches/NNN-description.patch
     difficulty: easy|medium|hard
     title: "Short description"
     description: "What the bug is"
     expected_findings:
       - title_pattern: "regex matching expected finding title"
         file_pattern: "file\\.py"
         line_range: [start, end]
         severity: critical|warning|nit
         category: correctness|security|reliability|concurrency|performance
         requires_context:  # optional
           - "path/to/related/file.py"
         recommendation: "Expected fix suggestion"
   ```

## Baseline & Regression Detection

- Baselines are saved as timestamped JSON in `eval/baselines/`
- Each run automatically compares against the latest baseline
- A **regression** is flagged when any case's composite score drops by >10%
- The CLI exits with code 1 when regressions are detected (useful for CI)

## Environment Variables

```bash
# Anthropic provider
ANTHROPIC_API_KEY=sk-ant-...

# Bedrock provider
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
AWS_DEFAULT_REGION=us-east-1

# OpenAI provider
OPENAI_API_KEY=sk-...
```
