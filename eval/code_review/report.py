"""Report generation, baseline comparison, and regression detection.

Supports two types of baselines:

1. **Self-baselines** (`baselines/`) — compare against your own previous runs.
   Detects regressions when pipeline quality drops after code changes.

2. **Gold-standard baselines** (`gold_baselines/`) — compare against a single
   powerful agent (e.g. Opus) with all tools and no pipeline constraints.
   Shows how close the pipeline gets to the quality ceiling.

Regression threshold: 10% drop in composite score flags a warning.
"""

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from scorer import CaseScore, compute_aggregate

BASELINES_DIR = Path(__file__).resolve().parent / "baselines"
GOLD_BASELINES_DIR = Path(__file__).resolve().parent / "gold_baselines"
REGRESSION_THRESHOLD = 0.10  # 10% drop triggers regression warning


@dataclass
class RegressionResult:
    """Result of comparing against a baseline."""
    case_id: str
    baseline_composite: float
    current_composite: float
    delta: float
    is_regression: bool

    def to_dict(self) -> dict:
        return {
            "case_id": self.case_id,
            "baseline_composite": round(self.baseline_composite, 3),
            "current_composite": round(self.current_composite, 3),
            "delta": round(self.delta, 3),
            "is_regression": self.is_regression,
        }


@dataclass
class GoldComparison:
    """Comparison of a pipeline run against the gold-standard baseline."""
    case_id: str
    gold_composite: float
    pipeline_composite: float
    delta: float            # pipeline - gold (negative = pipeline is worse)
    pct_of_gold: float      # pipeline / gold as percentage

    def to_dict(self) -> dict:
        return {
            "case_id": self.case_id,
            "gold_composite": round(self.gold_composite, 3),
            "pipeline_composite": round(self.pipeline_composite, 3),
            "delta": round(self.delta, 3),
            "pct_of_gold": round(self.pct_of_gold, 1),
        }


@dataclass
class EvalReport:
    """Complete evaluation report."""
    timestamp: str
    provider: str
    model: str
    mode: str = "pipeline"   # "pipeline" or "gold"
    case_scores: List[dict] = field(default_factory=list)
    judge_verdicts: List[dict] = field(default_factory=list)
    aggregate: Dict[str, float] = field(default_factory=dict)
    regressions: List[dict] = field(default_factory=list)
    has_regressions: bool = False
    gold_comparisons: List[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "provider": self.provider,
            "model": self.model,
            "mode": self.mode,
            "case_scores": self.case_scores,
            "judge_verdicts": self.judge_verdicts,
            "aggregate": self.aggregate,
            "regressions": self.regressions,
            "has_regressions": self.has_regressions,
            "gold_comparisons": self.gold_comparisons,
        }


def build_report(
    scores: List[CaseScore],
    judge_verdicts: Optional[List[dict]] = None,
    provider: str = "",
    model: str = "",
    mode: str = "pipeline",
    gold_baseline: Optional[dict] = None,
) -> EvalReport:
    """Build a complete eval report from scored cases.

    Args:
        scores: List of CaseScore from the scorer.
        judge_verdicts: Optional list of judge verdict dicts.
        provider: Provider name used for the run.
        model: Model ID used for the run.
        mode: "pipeline" or "gold".
        gold_baseline: Optional gold baseline dict for comparison.

    Returns:
        EvalReport with all metrics.
    """
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    report = EvalReport(
        timestamp=now,
        provider=provider,
        model=model,
        mode=mode,
        case_scores=[s.to_dict() for s in scores],
        judge_verdicts=judge_verdicts or [],
        aggregate=compute_aggregate(scores),
    )

    # Check for regressions against latest self-baseline
    baseline = load_latest_baseline()
    if baseline:
        regressions = detect_regressions(scores, baseline)
        report.regressions = [r.to_dict() for r in regressions]
        report.has_regressions = any(r.is_regression for r in regressions)

    # Compare against gold baseline if provided
    if gold_baseline:
        comparisons = compare_to_gold(scores, gold_baseline)
        report.gold_comparisons = [c.to_dict() for c in comparisons]

    return report


# ---------------------------------------------------------------------------
# Self-baseline: save / load / detect regressions
# ---------------------------------------------------------------------------

def save_baseline(report: EvalReport) -> str:
    """Save the report as a timestamped self-baseline.

    Args:
        report: Completed EvalReport to persist as a baseline.

    Returns:
        Absolute path to the saved baseline JSON file.
    """
    BASELINES_DIR.mkdir(parents=True, exist_ok=True)

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    filename = f"baseline_{ts}.json"
    filepath = BASELINES_DIR / filename

    data = report.to_dict()
    filepath.write_text(json.dumps(data, indent=2) + "\n")

    return str(filepath)


def load_latest_baseline() -> Optional[dict]:
    """Load the most recent self-baseline file.

    Returns:
        Parsed JSON dict of the latest baseline, or None if no baselines exist.
    """
    if not BASELINES_DIR.exists():
        return None

    baselines = sorted(BASELINES_DIR.glob("baseline_*.json"))
    if not baselines:
        return None

    latest = baselines[-1]
    return json.loads(latest.read_text())


def detect_regressions(
    current_scores: List[CaseScore],
    baseline: dict,
) -> List[RegressionResult]:
    """Compare current scores against a self-baseline.

    Flags regressions when composite drops by more than REGRESSION_THRESHOLD
    (10%). Only cases present in both the baseline and the current run are
    compared — new or removed cases are silently ignored.

    Args:
        current_scores: List of CaseScore objects from the current run.
        baseline: Parsed baseline dict (from load_latest_baseline).

    Returns:
        List of RegressionResult, one per case present in both runs.
        Check result.is_regression to identify failing cases.
    """
    baseline_scores = {}
    for cs in baseline.get("case_scores", []):
        baseline_scores[cs["case_id"]] = cs.get("composite", 0.0)

    results = []
    for score in current_scores:
        if score.case_id not in baseline_scores:
            continue

        bl = baseline_scores[score.case_id]
        delta = score.composite - bl
        is_regression = delta < -REGRESSION_THRESHOLD

        results.append(RegressionResult(
            case_id=score.case_id,
            baseline_composite=bl,
            current_composite=score.composite,
            delta=delta,
            is_regression=is_regression,
        ))

    return results


# ---------------------------------------------------------------------------
# Gold-standard baseline: save / load / compare
# ---------------------------------------------------------------------------

def save_gold_baseline(report: EvalReport) -> str:
    """Save the report as a timestamped gold-standard baseline.

    Args:
        report: Completed EvalReport from a gold-standard run to persist.

    Returns:
        Absolute path to the saved gold baseline JSON file.
    """
    GOLD_BASELINES_DIR.mkdir(parents=True, exist_ok=True)

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    filename = f"gold_{ts}.json"
    filepath = GOLD_BASELINES_DIR / filename

    data = report.to_dict()
    filepath.write_text(json.dumps(data, indent=2) + "\n")

    return str(filepath)


def load_latest_gold_baseline() -> Optional[dict]:
    """Load the most recent gold-standard baseline file.

    Returns:
        Parsed JSON dict of the latest gold baseline, or None if no gold
        baselines exist in the gold_baselines/ directory.
    """
    if not GOLD_BASELINES_DIR.exists():
        return None

    baselines = sorted(GOLD_BASELINES_DIR.glob("gold_*.json"))
    if not baselines:
        return None

    latest = baselines[-1]
    return json.loads(latest.read_text())


def compare_to_gold(
    current_scores: List[CaseScore],
    gold_baseline: dict,
) -> List[GoldComparison]:
    """Compare pipeline scores against the gold-standard baseline.

    Args:
        current_scores: List of CaseScore objects from the current pipeline run.
        gold_baseline: Parsed gold baseline dict (from load_latest_gold_baseline).

    Returns:
        List of GoldComparison, one per case present in both the current
        run and the gold baseline. Each entry reports the absolute delta
        and the pipeline score as a percentage of the gold ceiling.
    """
    gold_scores = {}
    for cs in gold_baseline.get("case_scores", []):
        gold_scores[cs["case_id"]] = cs.get("composite", 0.0)

    comparisons = []
    for score in current_scores:
        if score.case_id not in gold_scores:
            continue

        gold = gold_scores[score.case_id]
        delta = score.composite - gold
        pct = (score.composite / gold * 100) if gold > 0 else 0.0

        comparisons.append(GoldComparison(
            case_id=score.case_id,
            gold_composite=gold,
            pipeline_composite=score.composite,
            delta=delta,
            pct_of_gold=pct,
        ))

    return comparisons


# ---------------------------------------------------------------------------
# Report printing
# ---------------------------------------------------------------------------

def print_report(report: EvalReport) -> None:
    """Print a human-readable report to stdout.

    Outputs per-case scores, aggregate metrics, optional LLM judge verdicts,
    self-baseline regression detection, and gold-standard comparison table.

    Args:
        report: Completed EvalReport from build_report().
    """
    mode_label = "Gold-Standard" if report.mode == "gold" else "Pipeline"

    print(f"\n{'=' * 70}")
    print(f"  Code Review Eval Report ({mode_label}) — {report.timestamp}")
    print(f"  Provider: {report.provider}  Model: {report.model}")
    print(f"{'=' * 70}\n")

    # Per-case scores
    print("Per-Case Scores:")
    print(f"{'Case':<20} {'Recall':>8} {'Prec':>8} {'Sev':>8} {'Loc':>8} {'Rec':>8} {'Ctx':>8} {'Comp':>8}")
    print("-" * 88)

    for cs in report.case_scores:
        case_id = cs["case_id"]
        if cs.get("error"):
            print(f"{case_id:<20} {'ERROR':>8}")
            continue
        print(
            f"{case_id:<20} "
            f"{cs['recall']:>8.3f} "
            f"{cs['precision']:>8.3f} "
            f"{cs['severity_accuracy']:>8.3f} "
            f"{cs['location_accuracy']:>8.3f} "
            f"{cs['recommendation_score']:>8.3f} "
            f"{cs['context_depth']:>8.3f} "
            f"{cs['composite']:>8.3f}"
        )

    # Aggregate
    agg = report.aggregate
    print(f"\n{'Aggregate':<20} ", end="")
    for key in ["recall", "precision", "severity_accuracy", "location_accuracy",
                 "recommendation_score", "context_depth", "composite"]:
        print(f"{agg.get(key, 0.0):>8.3f} ", end="")
    print()

    # Judge verdicts
    if report.judge_verdicts:
        print(f"\n{'LLM Judge Verdicts':}")
        print(f"{'Case':<20} {'Compl':>8} {'Reason':>8} {'Action':>8} {'FP':>8} {'Avg':>8}")
        print("-" * 60)
        for v in report.judge_verdicts:
            print(
                f"{v.get('case_id', '?'):<20} "
                f"{v.get('completeness', 0):>8} "
                f"{v.get('reasoning_quality', 0):>8} "
                f"{v.get('actionability', 0):>8} "
                f"{v.get('false_positive_quality', 0):>8} "
                f"{v.get('average', 0.0):>8.2f}"
            )

    # Self-baseline regressions
    if report.regressions:
        print(f"\nRegression Detection (threshold: {REGRESSION_THRESHOLD * 100:.0f}%):")
        for r in report.regressions:
            marker = "REGRESSION" if r["is_regression"] else "ok"
            print(
                f"  {r['case_id']:<20} "
                f"baseline={r['baseline_composite']:.3f} "
                f"current={r['current_composite']:.3f} "
                f"delta={r['delta']:+.3f} "
                f"[{marker}]"
            )

    if report.has_regressions:
        print(f"\n*** REGRESSIONS DETECTED — review composite scores above ***")

    # Gold-standard comparison
    if report.gold_comparisons:
        print(f"\nGold-Standard Comparison:")
        print(f"{'Case':<20} {'Gold':>8} {'Pipeline':>8} {'Delta':>8} {'% of Gold':>10}")
        print("-" * 60)
        for c in report.gold_comparisons:
            print(
                f"  {c['case_id']:<20} "
                f"{c['gold_composite']:>8.3f} "
                f"{c['pipeline_composite']:>8.3f} "
                f"{c['delta']:>+8.3f} "
                f"{c['pct_of_gold']:>9.1f}%"
            )
        # Summary line
        avg_pct = sum(c["pct_of_gold"] for c in report.gold_comparisons) / len(report.gold_comparisons)
        print(f"\n  Average pipeline quality: {avg_pct:.1f}% of gold standard")

    print()
