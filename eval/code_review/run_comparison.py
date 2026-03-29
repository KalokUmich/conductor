#!/usr/bin/env python3
"""Run 3 rounds of Legacy vs Brain eval and print comparison table."""
import asyncio
import json
import os
import sys
import time
import yaml
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent
_BACKEND = str(_ROOT / "backend")
_EVAL_DIR = Path(__file__).resolve().parent
sys.path.insert(0, _BACKEND)
sys.path.insert(0, str(_EVAL_DIR))

# Load credentials
secrets = yaml.safe_load(open(str(_ROOT / "config/conductor.secrets.yaml")))
creds = secrets["ai_providers"]["aws_bedrock"]
os.environ.update({
    "AWS_ACCESS_KEY_ID": creds["access_key_id"],
    "AWS_SECRET_ACCESS_KEY": creds["secret_access_key"],
    "AWS_SESSION_TOKEN": creds["session_token"],
    "AWS_DEFAULT_REGION": creds["region"],
})

from runner import CaseConfig, run_case, run_case_brain
from scorer import score_case

from app.ai_provider.claude_bedrock import ClaudeBedrockProvider

kw = dict(
    aws_access_key_id=creds["access_key_id"],
    aws_secret_access_key=creds["secret_access_key"],
    aws_session_token=creds["session_token"],
    region_name=creds["region"],
)
sonnet = ClaudeBedrockProvider(model_id="eu.anthropic.claude-sonnet-4-6", **kw)
haiku = ClaudeBedrockProvider(model_id="eu.anthropic.claude-haiku-4-5-20251001-v1:0", **kw)

# Load cases
cases_yaml = yaml.safe_load(open(str(_EVAL_DIR / "cases/requests/cases.yaml")))
source_dir = str(_EVAL_DIR / "repos/requests")
patch_dir = str(_EVAL_DIR / "cases/requests")

CASES = []
for cd in cases_yaml["cases"]:
    CASES.append(CaseConfig(
        id=cd["id"], patch=cd["patch"], difficulty=cd["difficulty"],
        title=cd["title"], description=cd["description"],
        expected_findings=cd.get("expected_findings", []),
    ))

ROUNDS = 3


async def run_one_round(mode: str, round_num: int) -> dict:
    """Run all 12 cases in one mode. Returns {case_id: composite}."""
    results = {}
    for case in CASES:
        label = f"  [{mode} R{round_num}] {case.id}"
        print(f"{label}... ", end="", flush=True)
        try:
            if mode == "brain":
                r = await run_case_brain(case, source_dir, patch_dir, sonnet, haiku)
            else:
                r = await run_case(case, source_dir, patch_dir, sonnet, haiku, max_agents=5)

            if r.error:
                print(f"ERROR")
                results[case.id] = {"composite": 0.0, "error": r.error}
                continue

            review = r.review_result
            findings = review.findings if review else []
            files_reviewed = review.files_reviewed if review else []
            score = score_case(case, findings, files_reviewed)
            print(f"composite={score.composite:.3f} (findings={len(findings)})")
            results[case.id] = {
                "composite": score.composite,
                "recall": score.recall,
                "precision": score.precision,
                "severity": score.severity_accuracy,
                "location": score.location_accuracy,
                "recommendation": score.recommendation_score,
                "context": score.context_depth,
                "findings": len(findings),
            }
        except Exception as e:
            print(f"EXCEPTION: {e}")
            results[case.id] = {"composite": 0.0, "error": str(e)}
    return results


async def main():
    """Run 3 rounds of Legacy vs Brain eval and print a comparison table.

    Runs all 12 cases in both ``legacy`` (CodeReviewService) and ``brain``
    (PRBrainOrchestrator) modes for each round, then prints a per-case and
    aggregate comparison table with dimension-level averages.
    """
    all_results = {"legacy": [], "brain": []}

    for round_num in range(1, ROUNDS + 1):
        print(f"\n{'='*60}")
        print(f"  ROUND {round_num}/{ROUNDS}")
        print(f"{'='*60}")

        # Run legacy and brain sequentially to avoid throttling
        for mode in ["legacy", "brain"]:
            start = time.time()
            results = await run_one_round(mode, round_num)
            elapsed = time.time() - start
            all_results[mode].append(results)
            avg = sum(r.get("composite", 0) for r in results.values()) / len(results)
            print(f"  {mode} R{round_num}: avg composite={avg:.3f} ({elapsed:.0f}s)")

    # Print comparison table
    print(f"\n{'='*80}")
    print(f"  COMPARISON TABLE — {ROUNDS} rounds × 12 cases")
    print(f"{'='*80}\n")

    case_ids = [c.id for c in CASES]

    # Per-case average
    print(f"{'Case':<18}", end="")
    for r in range(ROUNDS):
        print(f" {'L'+str(r+1):>6} {'B'+str(r+1):>6}", end="")
    print(f" {'L_avg':>7} {'B_avg':>7} {'Delta':>7}")
    print("-" * (18 + ROUNDS * 13 + 22))

    legacy_totals = []
    brain_totals = []

    for cid in case_ids:
        print(f"{cid:<18}", end="")
        l_scores = []
        b_scores = []
        for r in range(ROUNDS):
            lc = all_results["legacy"][r].get(cid, {}).get("composite", 0)
            bc = all_results["brain"][r].get(cid, {}).get("composite", 0)
            l_scores.append(lc)
            b_scores.append(bc)
            print(f" {lc:>6.3f} {bc:>6.3f}", end="")

        l_avg = sum(l_scores) / len(l_scores)
        b_avg = sum(b_scores) / len(b_scores)
        delta = b_avg - l_avg
        legacy_totals.append(l_avg)
        brain_totals.append(b_avg)
        winner = "Brain" if delta > 0.02 else ("Legacy" if delta < -0.02 else "")
        print(f" {l_avg:>7.3f} {b_avg:>7.3f} {delta:>+7.3f} {winner}")

    # Aggregate
    l_agg = sum(legacy_totals) / len(legacy_totals)
    b_agg = sum(brain_totals) / len(brain_totals)
    delta_agg = b_agg - l_agg
    print("-" * (18 + ROUNDS * 13 + 22))
    print(f"{'AGGREGATE':<18}", end="")
    for r in range(ROUNDS):
        lc = sum(all_results["legacy"][r].get(cid, {}).get("composite", 0) for cid in case_ids) / len(case_ids)
        bc = sum(all_results["brain"][r].get(cid, {}).get("composite", 0) for cid in case_ids) / len(case_ids)
        print(f" {lc:>6.3f} {bc:>6.3f}", end="")
    print(f" {l_agg:>7.3f} {b_agg:>7.3f} {delta_agg:>+7.3f}")

    # Dimension averages
    print(f"\n{'DIMENSION AVERAGES':}")
    dims = ["recall", "precision", "severity", "location", "recommendation", "context"]
    print(f"{'Dimension':<18} {'Legacy':>8} {'Brain':>8} {'Delta':>8}")
    print("-" * 44)
    for dim in dims:
        l_vals = []
        b_vals = []
        for r in range(ROUNDS):
            for cid in case_ids:
                l_vals.append(all_results["legacy"][r].get(cid, {}).get(dim, 0))
                b_vals.append(all_results["brain"][r].get(cid, {}).get(dim, 0))
        l_mean = sum(l_vals) / len(l_vals) if l_vals else 0
        b_mean = sum(b_vals) / len(b_vals) if b_vals else 0
        delta = b_mean - l_mean
        print(f"{dim:<18} {l_mean:>8.3f} {b_mean:>8.3f} {delta:>+8.3f}")

    print(f"\n  Brain wins: {sum(1 for l, b in zip(legacy_totals, brain_totals) if b > l + 0.02)}/12 cases")
    print(f"  Legacy wins: {sum(1 for l, b in zip(legacy_totals, brain_totals) if l > b + 0.02)}/12 cases")
    print(f"  Tied: {sum(1 for l, b in zip(legacy_totals, brain_totals) if abs(b - l) <= 0.02)}/12 cases")


if __name__ == "__main__":
    asyncio.run(main())
