"""Issue deduplication and merge layer.

Multiple agents often find the same root cause from different perspectives.
This layer merges overlapping findings, keeping the strongest evidence.
"""

from __future__ import annotations

import logging
import re
from typing import List, Set

from .models import ReviewFinding, Severity

logger = logging.getLogger(__name__)

# Keywords that indicate the same root-cause domain.  If two findings in the
# same file share a domain tag, they're likely about the same issue even if
# their line ranges don't overlap.
_DOMAIN_TAGS: List[Set[str]] = [
    {"token", "consume", "replay", "reuse", "one-time", "webhook"},
    {"race", "concurrent", "lock", "atomic", "duplicate", "idempotent"},
    {"queue", "message", "send", "pending", "dlq", "retry"},
    {"test", "coverage", "untested", "missing test"},
    {"null", "nullable", "npe", "nullpointer"},
    {"validation", "validate", "input", "sanitize"},
]


def _extract_keywords(text: str) -> Set[str]:
    """Extract lowercase keyword tokens from a string."""
    return set(re.findall(r"[a-z]{3,}", text.lower()))


def _same_domain(a: ReviewFinding, b: ReviewFinding) -> bool:
    """Check if two findings belong to the same root-cause domain.

    Combines title + risk text for keyword matching.
    """
    a_kw = _extract_keywords(f"{a.title} {a.risk}")
    b_kw = _extract_keywords(f"{b.title} {b.risk}")

    return any(a_kw & domain and b_kw & domain for domain in _DOMAIN_TAGS)


def _findings_overlap(a: ReviewFinding, b: ReviewFinding) -> bool:
    """Check if two findings refer to the same issue.

    Overlap criteria (any one is enough):
      1. Same file + overlapping line ranges
      2. Same file + similar title (>50% word overlap)
      3. Same file + same root-cause domain (keyword matching)
      4. Same file + line ranges within 60 lines of each other
         (nearby code, likely related)
    """
    if not a.file or not b.file:
        return False

    if a.file != b.file:
        return False

    # Line range overlap
    if (
        a.start_line
        and b.start_line
        and a.end_line
        and b.end_line
        and a.start_line <= b.end_line
        and b.start_line <= a.end_line
    ):
        return True

    # Title similarity (word overlap)
    a_words = set(a.title.lower().split())
    b_words = set(b.title.lower().split())
    if a_words and b_words:
        overlap = len(a_words & b_words)
        smaller = min(len(a_words), len(b_words))
        if smaller > 0 and overlap / smaller > 0.5:
            return True

    # Same root-cause domain
    if _same_domain(a, b):
        return True

    # Nearby line ranges (within 60 lines)
    if a.start_line and b.start_line:
        gap = abs(a.start_line - b.start_line)
        if gap <= 60:
            return True

    return False


def _merge_pair(primary: ReviewFinding, secondary: ReviewFinding) -> ReviewFinding:
    """Merge two overlapping findings, keeping the stronger one.

    The primary finding is kept, enriched with evidence from secondary.
    """
    # Merge evidence (deduplicated)
    existing = set(primary.evidence)
    for e in secondary.evidence:
        if e not in existing:
            primary.evidence.append(e)
            existing.add(e)

    # Keep higher severity
    severity_order = [Severity.NIT, Severity.PRAISE, Severity.WARNING, Severity.CRITICAL]
    if severity_order.index(secondary.severity) > severity_order.index(primary.severity):
        primary.severity = secondary.severity

    # Keep higher confidence
    primary.confidence = max(primary.confidence, secondary.confidence)

    # Expand line range
    if secondary.start_line and (not primary.start_line or secondary.start_line < primary.start_line):
        primary.start_line = secondary.start_line
    if secondary.end_line and (not primary.end_line or secondary.end_line > primary.end_line):
        primary.end_line = secondary.end_line

    # Append agent attribution
    if secondary.agent and secondary.agent != primary.agent:
        primary.agent = f"{primary.agent}+{secondary.agent}"

    return primary


def dedup_findings(findings: List[ReviewFinding]) -> List[ReviewFinding]:
    """Deduplicate and merge overlapping findings.

    Finds clusters of overlapping findings and merges each cluster
    into a single finding with combined evidence and the highest severity.
    """
    if len(findings) <= 1:
        return findings

    # Sort by score descending so higher-quality findings are primary
    sorted_findings = sorted(findings, key=lambda f: f.score(), reverse=True)

    merged: List[ReviewFinding] = []
    consumed = set()

    for i, primary in enumerate(sorted_findings):
        if i in consumed:
            continue

        # Find all overlapping findings
        for j in range(i + 1, len(sorted_findings)):
            if j in consumed:
                continue
            if _findings_overlap(primary, sorted_findings[j]):
                primary = _merge_pair(primary, sorted_findings[j])
                consumed.add(j)

        merged.append(primary)

    logger.info(
        "Dedup: %d findings → %d after merging",
        len(findings),
        len(merged),
    )
    return merged
