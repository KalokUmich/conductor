"""Report generation, baseline comparison, and regression detection.

Saves timestamped JSON baselines and compares new runs against them.
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
class EvalReport:
    """Complete evaluation report."""
    timestamp: str
    provider: str
    model: str
    case_scores: List[dict] = field(default_factory=list)
    judge_verdicts: List[dict] = field(default_factory=list)
    aggregate: Dict[str, float] = field(default_factory=dict)
    regressions: List[dict] = field(default_factory=list)
    has_regressions: bool = False

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "provider": self.provider,
            "model": self.model,
            "case_scores": self.case_scores,
            "judge_verdicts": self.judge_verdicts,
            "aggregate": self.aggregate,
            "regressions": self.regressions,
            "has_regressions": self.has_regressions,
        }


def build_report(
    scores: List[CaseScore],
    judge_verdicts: Optional[List[dict]] = None,
    provider: str = "",
    model: str = "",
) -> EvalReport:
    """Build a complete eval report from scored cases.

    Args:
        scores: List of CaseScore from the scorer.
        judge_verdicts: Optional list of judge verdict dicts.
        provider: Provider name used for the run.
        model: Model ID used for the run.

    Returns:
        EvalReport with all metrics.
    """
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    report = EvalReport(
        timestamp=now,
        provider=provider,
        model=model,
        case_scores=[s.to_dict() for s in scores],
        judge_verdicts=judge_verdicts or [],
        aggregate=compute_aggregate(scores),
    )

    # Check for regressions against latest baseline
    baseline = load_latest_baseline()
    if baseline:
        regressions = detect_regressions(scores, baseline)
        report.regressions = [r.to_dict() for r in regressions]
        report.has_regressions = any(r.is_regression for r in regressions)

    return report


def save_baseline(report: EvalReport) -> str:
    """Save the report as a timestamped baseline.

    Returns:
        Path to the saved baseline file.
    """
    BASELINES_DIR.mkdir(parents=True, exist_ok=True)

    # Use a filesystem-safe timestamp
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    filename = f"baseline_{ts}.json"
    filepath = BASELINES_DIR / filename

    data = report.to_dict()
    filepath.write_text(json.dumps(data, indent=2) + "\n")

    return str(filepath)


def load_latest_baseline() -> Optional[dict]:
    """Load the most recent baseline file.

    Returns:
        Parsed baseline dict, or None if no baselines exist.
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
    """Compare current scores against a baseline and detect regressions.

    A regression is flagged when a case's composite score drops by more
    than REGRESSION_THRESHOLD (10%) compared to the baseline.

    Args:
        current_scores: Current run's case scores.
        baseline: Previous baseline dict.

    Returns:
        List of RegressionResult for each case that was in both runs.
    """
    # Build lookup from baseline
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


def print_report(report: EvalReport) -> None:
    """Print a human-readable report to stdout."""
    print(f"\n{'=' * 70}")
    print(f"  Code Review Eval Report — {report.timestamp}")
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

    # Regressions
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

    print()
