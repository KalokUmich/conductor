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
    # severity_match is a graded float, not a bool: 1.0 for exact match,
    # 0.5 for adjacent severity (one step apart on the ordinal scale —
    # e.g. critical vs high), 0.0 otherwise. Adjacent partial credit
    # reflects that two competent reviewers reasonably disagree by one
    # severity level on borderline findings.
    severity_match: float = 0.0
    category_match: bool = False
    recommendation_match: bool = False


# Severity ordinal scale used for adjacency scoring. WARNING is the
# deprecated alias for MEDIUM and shares its rank.
_SEVERITY_RANK = {
    "nit": 0,
    "low": 1,
    "medium": 2,
    "warning": 2,
    "high": 3,
    "critical": 4,
}


def _severity_score(actual: str, expected: str) -> float:
    """Score severity match: 1.0 exact, 0.5 adjacent, 0.0 otherwise.

    'praise' is excluded from adjacency — it's a different category
    (positive feedback) and never scores partial credit against a
    real-finding severity.
    """
    if not actual or not expected:
        return 0.0
    a, e = actual.lower(), expected.lower()
    if a == e:
        return 1.0
    # warning ≡ medium equivalence
    if {a, e} == {"warning", "medium"}:
        return 1.0
    # praise has no adjacency — it's not on the bug-severity scale
    if "praise" in (a, e):
        return 0.0
    rank_a = _SEVERITY_RANK.get(a)
    rank_e = _SEVERITY_RANK.get(e)
    if rank_a is None or rank_e is None:
        return 0.0
    if abs(rank_a - rank_e) == 1:
        return 0.5
    return 0.0


@dataclass
class CaseScore:
    """Scores for a single eval case."""

    case_id: str
    recall: float = 0.0  # fraction of expected findings matched
    precision: float = 0.0  # fraction of actual findings that are true positives
    severity_accuracy: float = 0.0
    location_accuracy: float = 0.0
    recommendation_score: float = 0.0
    context_depth: float = 0.0
    catch_rate: float = 0.0  # 1.0 if ANY expected finding was matched on
    # title+file+line, else 0.0 — Greptile-style
    # binary "did the reviewer catch the bug"
    catch_fraction: float = 0.0  # fraction of expected bugs caught (file+line) — the
    # honest per-bug catch on multi-finding cases, where
    # the binary catch_rate over-states (1 of 5 == 5 of 5)
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
            "catch_rate": round(self.catch_rate, 3),
            "catch_fraction": round(self.catch_fraction, 3),
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

    # Recall: fraction of expected findings matched WITH A REAL LOCATION SIGNAL.
    # A pairing counts as "found the bug" only if it lands in the right file and
    # corroborates with either the line range or the title. A title-only hit in
    # the wrong file, or a bare severity+category coincidence (both of which stay
    # eligible for *assignment*), no longer inflates recall — they carry zero
    # location agreement. See the 2026-06-02 scoring audit: loose stopword
    # title_patterns + sev+cat eligibility let a reviewer who located nothing
    # score recall up to 1.0 while catch_rate honestly stayed 0.
    matched_expected = set(m.expected_index for m in matches if m.file_match and (m.line_match or m.title_match))
    score.recall = len(matched_expected) / len(expected)

    # Catch rate (Greptile-style): 1.0 if AT LEAST ONE expected finding was
    # matched with a real line-level pointer (title + file + line), else 0.0.
    # Severity / category mismatches do NOT zero out the catch — Greptile's
    # rubric is: "did the reviewer leave an explicit line-level PR comment
    # that points to the faulty code". A finding that points at the right
    # file:line counts even if the title is generic.
    score.catch_rate = 1.0 if any(m.file_match and m.line_match for m in matches) else 0.0

    # Catch FRACTION (honest per-bug catch): the share of EXPECTED bugs caught
    # with a real line-level pointer. On a 5-bug case, finding 1 scores 0.2 here
    # vs 1.0 on the binary catch_rate above. This is the number to trust on
    # multi-finding cases and the basis of the greptile_view aggregate.
    score.catch_fraction = len({m.expected_index for m in matches if m.file_match and m.line_match}) / len(expected)

    # Precision: fraction of actual findings that matched an expected finding
    # Findings that don't match any expected finding are considered false positives,
    # but we're lenient — we only penalize if there are many more findings than expected.
    matched_actual = set(m.actual_index for m in matches)
    if findings:
        # Use a soft precision: don't penalize extra findings too harshly
        # since the reviewer might find legitimate issues beyond our ground truth.
        # Praise / positive-feedback findings are NOT bug claims, so they are
        # excluded from the denominator — they can't be false positives.
        true_positives = len(matched_actual)
        total = sum(1 for f in findings if not _is_praise(f) and not _is_dismissal(f))
        # Cap false positive penalty: at most 50% of extra findings count against precision
        false_positives = max(0, total - true_positives)
        effective_fp = false_positives * 0.5
        score.precision = (
            true_positives / (true_positives + effective_fp) if (true_positives + effective_fp) > 0 else 0.0
        )

    # Severity accuracy: average graded severity score across matched
    # findings (1.0 exact, 0.5 adjacent, 0.0 otherwise).
    if matches:
        score.severity_accuracy = sum(m.severity_match for m in matches) / len(matches)

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

    Args:
        scores: List of CaseScore objects, one per evaluated case.

    Returns:
        Dict with mean scores per dimension and overall composite.
        Keys: recall, precision, severity_accuracy, location_accuracy,
        recommendation_score, context_depth, composite, cases_total,
        cases_scored, cases_errored.
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
        "catch_rate": round(sum(s.catch_rate for s in valid) / n, 3),
        "catch_fraction": round(sum(s.catch_fraction for s in valid) / n, 3),
        # greptile_view: the metric most comparable to Greptile's own benchmark —
        # mean per-bug catch (line-level), severity/precision excluded. HEADLINE
        # this when comparing to Greptile; the 6-axis composite is our internal
        # product metric, not comparable to Greptile's ~82%.
        "greptile_view": round(sum(s.catch_fraction for s in valid) / n, 3),
        "composite": round(sum(s.composite for s in valid) / n, 3),
        "cases_total": len(scores),
        "cases_scored": n,
        "cases_errored": len(scores) - n,
    }


def _is_praise(finding) -> bool:
    """True if the finding is praise / positive feedback, not a defect claim.

    Praise findings must never be matched against an expected BUG, and must
    not count as false positives when scoring precision — a reviewer noting
    good code is not claiming a non-existent defect.
    """
    sev = getattr(finding, "severity", None)
    val = getattr(sev, "value", sev)
    return str(val).lower() == "praise"


# Phrases where a reviewer DENIES a defect rather than asserting one. The
# fixed-width negation lookbehinds keep real findings safe — "not correctly
# guarded" / "isn't a false positive" must NOT register as dismissals.
_DISMISSAL_RE = re.compile(
    # Fixed-width negation lookbehinds (incl. article-tolerant "not a "/"n't a ")
    # so a genuine defect phrased as a negation ("not correctly guarded",
    # "isn't a false positive") is NOT treated as a dismissal. Markers are kept
    # unambiguous on purpose — e.g. "is not an issue" (not bare "not an issue",
    # which would mis-fire on "not an issue-free path").
    r"(?<!not )(?<!n't )(?<!isn't )(?<!aren't )(?<!without )(?<!not a )(?<!n't a )"
    r"(?:"
    r"correctly guarded|properly guarded|"
    r"is mitigated|already mitigated|bug is mitigated|"
    r"not a bug|no bug here|false positive|already handled|"
    r"no fix needed|no fix required|no issue here|"
    r"no actual bug|no real bug|no vulnerability|not vulnerable"
    r")",
    re.IGNORECASE,
)


def _is_dismissal(finding) -> bool:
    """True if the finding DENIES a defect rather than asserting one.

    A reviewer who writes "integer overflow is correctly guarded — planted bug
    is mitigated" has NOT caught the bug; they argued it does not exist. Such a
    finding must not satisfy catch_rate / recall just because it points at the
    right file:line while claiming there is nothing to fix. Excluded from
    matching and from the precision denominator, mirroring _is_praise().
    See the 2026-06-02 scoring audit (keycloak-003 ASN1 case, where a
    "correctly guarded — mitigated" denial was credited as a catch).
    """
    text = " ".join(str(getattr(finding, attr, "") or "") for attr in ("title", "description", "suggested_fix"))
    return bool(_DISMISSAL_RE.search(text))


def _line_proximity(exp: dict, finding) -> float:
    """Tiebreak helper in [0, 1]: 1.0 when ranges overlap, decaying toward 0 as
    the line gap grows.

    Only used (at a tiny weight, below severity/category) to break ties between
    candidate actuals that are otherwise identical on the identity signals, so
    it can never override a real title/file/line match difference.
    """
    line_range = exp.get("line_range", [])
    if not (line_range and len(line_range) == 2 and getattr(finding, "start_line", 0) > 0):
        return 0.0
    exp_start, exp_end = line_range
    act_start = finding.start_line
    act_end = finding.end_line if getattr(finding, "end_line", 0) > 0 else finding.start_line
    if act_start <= exp_end and act_end >= exp_start:
        return 1.0
    gap = (exp_start - act_end) if act_end < exp_start else (act_start - exp_end)
    return 1.0 / (1.0 + abs(gap))


def _match_findings(expected: list, findings: list) -> List[FindingMatch]:
    """Match expected findings to actual findings.

    Uses **optimal one-to-one assignment** (max-weight bipartite matching),
    not greedy first-come matching. Greedy mis-pairs two findings in the same
    file when the strong title/file signals tie and only the line signal
    distinguishes them — that produced false catch=0 on cases where both bugs
    were actually found (e.g. a typo + a config finding in one file getting
    cross-paired so neither line range overlaps).

    The assignment score is **identity-first and layered by orders of
    magnitude** so a more-specific signal always dominates a weaker one:
    title >> (file+line "catch") >> file >> line >> severity/category >>
    line-proximity. This matters because the raw weighted sum lets
    severity+category (weight 2) outweigh a line match (weight 1), which would
    let even optimal assignment pick a location-wrong pairing. Pairing is about
    IDENTITY (which finding is about which bug), so location wins; severity and
    category only break ties. Eligibility still uses the original threshold
    (>= 2 = at least a title or file match). Each actual finding is matched at
    most once.

    Praise / positive-feedback findings are excluded from matching — they can
    never be the answer to an expected defect.
    """
    # Candidate actuals = non-praise, non-dismissal findings, keyed by original
    # index. Dismissals (explicit "this is NOT a bug" findings) are excluded like
    # praise: they must never satisfy a catch / recall by pointing at the right
    # file:line while denying any defect exists.
    cand = [(act_idx, f) for act_idx, f in enumerate(findings) if not _is_praise(f) and not _is_dismissal(f)]
    if not cand or not expected:
        return []

    # For every (expected, candidate) pair: the FindingMatch + its identity-first
    # assignment score. Only pairs that clear the original eligibility threshold
    # (raw score >= 2) are stored.
    cell: Dict[tuple, tuple] = {}
    for exp_idx, exp in enumerate(expected):
        for pos, (act_idx, finding) in enumerate(cand):
            m = _evaluate_match(exp_idx, act_idx, exp, finding)
            keep_score = m.title_match * 3 + m.file_match * 2 + m.line_match + m.severity_match + m.category_match
            if keep_score < 2:  # require at least a title or file match
                continue
            catch_pair = 1.0 if (m.file_match and m.line_match) else 0.0
            title_and_file = 1.0 if (m.title_match and m.file_match) else 0.0
            title_only = 1.0 if (m.title_match and not m.file_match) else 0.0
            # LOCATION-FIRST identity. An exact file+line CATCH is the definitive
            # signal that this finding IS the expected bug, so it must outrank a
            # title match — a title_pattern can be a common word (e.g. "should")
            # that hits an unrelated finding and would otherwise steal a correctly
            # located finding's match (observed: keycloak-009). A title match only
            # counts as strong identity when corroborated by the file; a bare title
            # hit in a different file is weak.
            assign_score = (
                catch_pair * 1000.0  # exact location = the bug
                + title_and_file * 300.0  # specific title corroborated by file
                + m.file_match * 50.0
                + m.line_match * 10.0
                + title_only * 5.0  # bare title in a different file = weak
                + m.severity_match * 1.0
                + m.category_match * 0.5
                + _line_proximity(exp, finding) * 0.01  # tiebreak only, < cat weight
            )
            cell[(exp_idx, pos)] = (m, assign_score)

    n_exp = len(expected)
    n_cand = len(cand)
    memo: Dict[tuple, tuple] = {}

    def solve(exp_idx: int, used_mask: int) -> tuple:
        """Return (total_assign_score, path) maximized over expected[exp_idx:]."""
        if exp_idx == n_exp:
            return (0.0, ())
        key = (exp_idx, used_mask)
        cached = memo.get(key)
        if cached is not None:
            return cached
        best = solve(exp_idx + 1, used_mask)  # leave expected[exp_idx] unmatched
        for pos in range(n_cand):
            if used_mask & (1 << pos):
                continue
            entry = cell.get((exp_idx, pos))
            if entry is None:
                continue
            _, assign_score = entry
            sub = solve(exp_idx + 1, used_mask | (1 << pos))
            cand_total = (assign_score + sub[0], ((exp_idx, pos),) + sub[1])
            if cand_total[0] > best[0]:
                best = cand_total
        memo[key] = best
        return best

    _, path = solve(0, 0)
    return [cell[(exp_idx, pos)][0] for (exp_idx, pos) in path]


def _evaluate_match(exp_idx: int, act_idx: int, expected: dict, finding) -> FindingMatch:
    """Evaluate how well a finding matches an expected finding.

    Args:
        exp_idx: Index of the expected finding in the case's expected_findings list.
        act_idx: Index of the actual finding in the review findings list.
        expected: Expected finding dict with title_pattern, file_pattern,
            line_range, severity, category, and recommendation keys.
        finding: Actual ReviewFinding object from the review.

    Returns:
        FindingMatch with boolean flags for each dimension (title, file,
        line, severity, category, recommendation).
    """
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
        act_end = finding.end_line if (getattr(finding, "end_line", 0) or 0) > 0 else finding.start_line
        m.line_match = act_start <= exp_end and act_end >= exp_start

    # Severity match — graded score on the 4-level scale. Handles the
    # deprecated "warning" alias (≡ medium) and gives 0.5 partial credit
    # for adjacent severities (e.g. agent=critical vs expected=high). See
    # _severity_score() for the rationale.
    exp_severity = expected.get("severity", "").lower()
    if exp_severity and hasattr(finding, "severity"):
        act_severity = finding.severity.value.lower()
        m.severity_match = _severity_score(act_severity, exp_severity)

    # Category match
    exp_category = expected.get("category", "")
    if exp_category and hasattr(finding, "category"):
        m.category_match = finding.category.value.lower() == exp_category.lower()

    # Recommendation match — check if suggested_fix contains key terms
    exp_rec = expected.get("recommendation", "")
    if exp_rec and finding.suggested_fix:
        # Extract key words from expected recommendation and check if any appear
        keywords = re.findall(r"\w{4,}", exp_rec.lower())
        fix_lower = finding.suggested_fix.lower()
        matched_kw = sum(1 for kw in keywords if kw in fix_lower)
        m.recommendation_match = matched_kw >= max(1, len(keywords) // 3)

    return m
