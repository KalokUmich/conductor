"""Scoring and ranking layer for review findings.

Assigns a final score to each finding based on severity, confidence,
evidence quality, and proximity to core business logic. Then sorts
findings by score to present the most important issues first.
"""

from __future__ import annotations

from typing import List

from .models import FileCategory, PRContext, ReviewFinding, Severity


def _evidence_quality(finding: ReviewFinding) -> float:
    """Score evidence quality 0.0–1.0.

    Higher for findings with specific file:line references and
    multiple evidence points.
    """
    score = 0.5  # base
    if finding.file and finding.start_line:
        score += 0.2
    if len(finding.evidence) >= 2:
        score += 0.2
    if finding.suggested_fix:
        score += 0.1
    return min(score, 1.0)


def _proximity_to_core(finding: ReviewFinding, pr_context: PRContext) -> float:
    """Score how close a finding is to the PR's core change flow.

    Findings in business logic files with the most changes get higher scores.
    """
    if not finding.file:
        return 0.5

    # Check if the file is in the PR
    for f in pr_context.files:
        if f.path == finding.file:
            if f.category == FileCategory.BUSINESS_LOGIC:
                # Larger changes = higher proximity
                change_size = f.additions + f.deletions
                if change_size > 100:
                    return 1.0
                if change_size > 30:
                    return 0.8
                return 0.6
            elif f.category == FileCategory.TEST:
                return 0.3
            elif f.category == FileCategory.CONFIG:
                return 0.5
            return 0.4

    # File not in the PR diff (found via impact analysis)
    return 0.3


def score_and_rank(
    findings: List[ReviewFinding],
    pr_context: PRContext,
) -> List[ReviewFinding]:
    """Score each finding and sort by descending score.

    Final Score = severity_weight × confidence × evidence_quality × proximity

    Praise findings are always ranked last.
    """
    severity_weight = {
        Severity.CRITICAL: 1.0,
        Severity.WARNING: 0.6,
        Severity.NIT: 0.2,
        Severity.PRAISE: 0.0,
    }

    scored = []
    for f in findings:
        sw = severity_weight.get(f.severity, 0.3)
        eq = _evidence_quality(f)
        px = _proximity_to_core(f, pr_context)
        final_score = sw * f.confidence * eq * px
        scored.append((final_score, f))

    # Sort: praise last, then by score descending
    scored.sort(
        key=lambda pair: (
            0 if pair[1].severity == Severity.PRAISE else 1,
            pair[0],
        ),
        reverse=True,
    )

    return [f for _, f in scored]
