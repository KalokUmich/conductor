"""Deterministic scoring for code review eval results.

Compares review findings against ground-truth expected findings using
pattern matching. Produces per-case and composite scores.

Weights:
  - Recall:         35%  (did the reviewer find the planted bugs?)
  - Precision:      20%  (what fraction of findings are real?)
  - Severity:       15%  (did it assign the right severity?)
  - Location:       10%  (did it point to the right file/lines?)
  - Recommendation: 10%  (did it suggest the right fix?)
  - Context:        10%  (did it explore cross-file dependencies?)
"""

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from runner import CaseConfig


@dataclass
class FindingMatch:
    """A match between an expected finding and an actual finding."""
    expected_index: int
    actual_index: int
    title_match: bool = False
    file_match: bool = False
    line_match: bool = False
    severity_match: bool = False
    category_match: bool = False
    recommendation_match: bool = False


@dataclass
class CaseScore:
    """Scores for a single eval case."""
    case_id: str
    recall: float = 0.0           # fraction of expected findings matched
    precision: float = 0.0        # fraction of actual findings that are true positives
    severity_accuracy: float = 0.0
    location_accuracy: float = 0.0
    recommendation_score: float = 0.0
    context_depth: float = 0.0
    composite: float = 0.0
    matches: List[FindingMatch] = field(default_factory=list)
    expected_count: int = 0
    actual_count: int = 0
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "case_id": self.case_id,
            "recall": round(self.recall, 3),
            "precision": round(self.precision, 3),
            "severity_accuracy": round(self.severity_accuracy, 3),
            "location_accuracy": round(self.location_accuracy, 3),
            "recommendation_score": round(self.recommendation_score, 3),
            "context_depth": round(self.context_depth, 3),
            "composite": round(self.composite, 3),
            "expected_count": self.expected_count,
            "actual_count": self.actual_count,
            "error": self.error,
        }


# Composite score weights
WEIGHTS = {
    "recall": 0.35,
    "precision": 0.20,
    "severity": 0.15,
    "location": 0.10,
    "recommendation": 0.10,
    "context": 0.10,
}


def score_case(case: CaseConfig, findings: list, files_reviewed: list) -> CaseScore:
    """Score review findings against expected ground truth.

    Args:
        case: Case config with expected_findings.
        findings: List of ReviewFinding objects from the review.
        files_reviewed: List of files the reviewer accessed.

    Returns:
        CaseScore with all dimension scores and composite.
    """
    expected = case.expected_findings
    if not expected:
        return CaseScore(case_id=case.id, error="No expected findings defined")

    score = CaseScore(
        case_id=case.id,
        expected_count=len(expected),
        actual_count=len(findings),
    )

    if not findings:
        score.composite = 0.0
        return score

    # Match expected findings to actual findings
    matches = _match_findings(expected, findings)
    score.matches = matches

    # Recall: fraction of expected findings that were matched
    matched_expected = set(m.expected_index for m in matches)
    score.recall = len(matched_expected) / len(expected)

    # Precision: fraction of actual findings that matched an expected finding
    # Findings that don't match any expected finding are considered false positives,
    # but we're lenient — we only penalize if there are many more findings than expected.
    matched_actual = set(m.actual_index for m in matches)
    if findings:
        # Use a soft precision: don't penalize extra findings too harshly
        # since the reviewer might find legitimate issues beyond our ground truth
        true_positives = len(matched_actual)
        total = len(findings)
        # Cap false positive penalty: at most 50% of extra findings count against precision
        false_positives = max(0, total - true_positives)
        effective_fp = false_positives * 0.5
        score.precision = true_positives / (true_positives + effective_fp) if (true_positives + effective_fp) > 0 else 0.0

    # Severity accuracy: among matched findings, how many got severity right
    if matches:
        severity_correct = sum(1 for m in matches if m.severity_match)
        score.severity_accuracy = severity_correct / len(matches)

    # Location accuracy: file + line range match
    if matches:
        location_scores = []
        for m in matches:
            loc = 0.0
            if m.file_match:
                loc += 0.5
            if m.line_match:
                loc += 0.5
            location_scores.append(loc)
        score.location_accuracy = sum(location_scores) / len(location_scores)

    # Recommendation score: among matched, how many had recommendation keywords
    if matches:
        rec_correct = sum(1 for m in matches if m.recommendation_match)
        score.recommendation_score = rec_correct / len(matches)

    # Context depth: did the reviewer explore required cross-file context?
    context_scores = []
    for exp in expected:
        requires = exp.get("requires_context", [])
        if not requires:
            context_scores.append(1.0)  # no cross-file requirement
            continue
        found = 0
        for req_file in requires:
            if any(req_file in f for f in files_reviewed):
                found += 1
        context_scores.append(found / len(requires) if requires else 1.0)
    score.context_depth = sum(context_scores) / len(context_scores) if context_scores else 0.0

    # Composite score
    score.composite = (
        WEIGHTS["recall"] * score.recall
        + WEIGHTS["precision"] * score.precision
        + WEIGHTS["severity"] * score.severity_accuracy
        + WEIGHTS["location"] * score.location_accuracy
        + WEIGHTS["recommendation"] * score.recommendation_score
        + WEIGHTS["context"] * score.context_depth
    )

    return score


def compute_aggregate(scores: List[CaseScore]) -> Dict[str, float]:
    """Compute aggregate metrics across all cases.

    Returns dict with mean scores per dimension and overall composite.
    """
    if not scores:
        return {"composite": 0.0}

    valid = [s for s in scores if s.error is None]
    if not valid:
        return {"composite": 0.0, "error_count": len(scores)}

    n = len(valid)
    return {
        "recall": round(sum(s.recall for s in valid) / n, 3),
        "precision": round(sum(s.precision for s in valid) / n, 3),
        "severity_accuracy": round(sum(s.severity_accuracy for s in valid) / n, 3),
        "location_accuracy": round(sum(s.location_accuracy for s in valid) / n, 3),
        "recommendation_score": round(sum(s.recommendation_score for s in valid) / n, 3),
        "context_depth": round(sum(s.context_depth for s in valid) / n, 3),
        "composite": round(sum(s.composite for s in valid) / n, 3),
        "cases_total": len(scores),
        "cases_scored": n,
        "cases_errored": len(scores) - n,
    }


def _match_findings(expected: list, findings: list) -> List[FindingMatch]:
    """Match expected findings to actual findings using pattern matching.

    Uses greedy matching: each expected finding matches the best available
    actual finding. An actual finding can only be matched once.
    """
    matches = []
    used_actual = set()

    for exp_idx, exp in enumerate(expected):
        best_match = None
        best_score = -1

        for act_idx, finding in enumerate(findings):
            if act_idx in used_actual:
                continue

            m = _evaluate_match(exp_idx, act_idx, exp, finding)
            match_score = sum([
                m.title_match * 3,  # title match is most important
                m.file_match * 2,
                m.line_match,
                m.severity_match,
                m.category_match,
            ])

            if match_score > best_score:
                best_score = match_score
                best_match = (act_idx, m)

        if best_match and best_score >= 2:  # require at least title or file match
            act_idx, m = best_match
            used_actual.add(act_idx)
            matches.append(m)

    return matches


def _evaluate_match(exp_idx: int, act_idx: int, expected: dict, finding) -> FindingMatch:
    """Evaluate how well a finding matches an expected finding."""
    m = FindingMatch(expected_index=exp_idx, actual_index=act_idx)

    # Title pattern match
    title_pattern = expected.get("title_pattern", "")
    if title_pattern:
        m.title_match = bool(re.search(title_pattern, finding.title, re.IGNORECASE))

    # File pattern match
    file_pattern = expected.get("file_pattern", "")
    if file_pattern:
        m.file_match = bool(re.search(file_pattern, finding.file, re.IGNORECASE))

    # Line range overlap
    line_range = expected.get("line_range", [])
    if line_range and len(line_range) == 2 and finding.start_line > 0:
        exp_start, exp_end = line_range
        # Check if there's any overlap between expected and actual line ranges
        act_start = finding.start_line
        act_end = finding.end_line if finding.end_line > 0 else finding.start_line
        m.line_match = act_start <= exp_end and act_end >= exp_start

    # Severity match
    exp_severity = expected.get("severity", "")
    if exp_severity and hasattr(finding, "severity"):
        m.severity_match = finding.severity.value.lower() == exp_severity.lower()

    # Category match
    exp_category = expected.get("category", "")
    if exp_category and hasattr(finding, "category"):
        m.category_match = finding.category.value.lower() == exp_category.lower()

    # Recommendation match — check if suggested_fix contains key terms
    exp_rec = expected.get("recommendation", "")
    if exp_rec and finding.suggested_fix:
        # Extract key words from expected recommendation and check if any appear
        keywords = re.findall(r'\w{4,}', exp_rec.lower())
        fix_lower = finding.suggested_fix.lower()
        matched_kw = sum(1 for kw in keywords if kw in fix_lower)
        m.recommendation_match = matched_kw >= max(1, len(keywords) // 3)

    return m
