"""Data models for the AI Code Review system.

Covers:
  - PRContext: structured representation of a PR diff
  - ChangedFile: per-file change metadata
  - RiskProfile: risk assessment across 5 dimensions
  - ReviewFinding: structured issue from a review agent
  - ReviewResult: aggregated multi-agent review output
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional

# ---------------------------------------------------------------------------
# PR context
# ---------------------------------------------------------------------------


class FileCategory(str, Enum):
    """File classification for review prioritization."""

    BUSINESS_LOGIC = "business_logic"  # services, controllers, models
    TEST = "test"  # test files
    CONFIG = "config"  # yml, properties, env
    INFRA = "infra"  # CI/CD, Dockerfile, terraform
    SCHEMA = "schema"  # DB migrations, schema files
    GENERATED = "generated"  # auto-generated, vendor, lock files
    OTHER = "other"


@dataclass
class ChangedFile:
    """A single file changed in the PR."""

    path: str
    status: str = "modified"  # modified, added, deleted, renamed
    additions: int = 0
    deletions: int = 0
    category: FileCategory = FileCategory.OTHER
    old_path: Optional[str] = None  # for renames


@dataclass
class PRContext:
    """Structured representation of a Pull Request."""

    diff_spec: str  # e.g. "main...feature/branch"
    files: List[ChangedFile] = field(default_factory=list)
    total_additions: int = 0
    total_deletions: int = 0
    total_changed_lines: int = 0
    file_count: int = 0
    # PR intent — populated by the caller (Azure DevOps router from
    # pr_data["title"]/["description"], eval runner from case.title/
    # case.description, chat flow from git log as fallback).
    # Empty strings when the caller didn't supply them; sub-agents must
    # degrade gracefully (v1 fixed swarm ignores; v2 coordinator includes
    # when present, falls back to diff-only intent inference when not).
    title: str = ""
    description: str = ""

    def business_logic_files(self) -> List[ChangedFile]:
        return [f for f in self.files if f.category == FileCategory.BUSINESS_LOGIC]

    def test_files(self) -> List[ChangedFile]:
        return [f for f in self.files if f.category == FileCategory.TEST]

    def config_files(self) -> List[ChangedFile]:
        return [f for f in self.files if f.category == FileCategory.CONFIG]

    def security_sensitive_files(self) -> List[ChangedFile]:
        """Files whose path patterns suggest security-critical surface area.

        Category-agnostic — catches e.g. an auth helper that was classified
        as INFRA or a migration adding a permissions table. Used by the
        security review sub-agent to widen its scope beyond business_logic
        files alone.
        """
        import re

        # Match a keyword preceded by a path separator (`/`, `_`, `-`, `.`) or
        # the start of string. Letting `_` and `-` count as separators catches
        # files like `0042_add_permissions_table.sql` and `session_store.py`
        # where the keyword is embedded in a snake_case filename but still
        # semantically meaningful. Avoids matching mid-word substrings like
        # "coauthor" where no separator precedes the keyword.
        pattern = re.compile(
            r"(?i)(?:^|[/._-])("
            r"auth|crypto|session|oauth|security|secret|token|credential|permission"
            r")"
        )
        return [f for f in self.files if pattern.search(f.path)]


# ---------------------------------------------------------------------------
# Risk classification
# ---------------------------------------------------------------------------


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass
class RiskProfile:
    """Risk assessment across 5 dimensions.

    Each dimension is scored low/medium/high/critical based on
    the types of changes detected in the PR.
    """

    correctness: RiskLevel = RiskLevel.LOW
    concurrency: RiskLevel = RiskLevel.LOW
    security: RiskLevel = RiskLevel.LOW
    reliability: RiskLevel = RiskLevel.LOW
    operational: RiskLevel = RiskLevel.LOW

    def max_risk(self) -> RiskLevel:
        """Return the highest risk level across all dimensions."""
        order = [RiskLevel.LOW, RiskLevel.MEDIUM, RiskLevel.HIGH, RiskLevel.CRITICAL]
        return max(
            [self.correctness, self.concurrency, self.security, self.reliability, self.operational],
            key=lambda r: order.index(r),
        )


# ---------------------------------------------------------------------------
# Review findings
# ---------------------------------------------------------------------------


class Severity(str, Enum):
    """4-level defect severity aligned with Greptile's benchmark scale.

    The 4 defect levels (critical → high → medium → low) replace the old
    3-level scale (critical / warning / nit).  ``WARNING`` is kept as a
    **deprecated alias** that the parser maps to ``MEDIUM`` so existing
    agent output and test fixtures keep working during the transition.

    Non-defect levels (``NIT``, ``PRAISE``) are retained: ``NIT`` is for
    style / improvement suggestions that the ``DO NOT FLAG`` list allows
    through, and ``PRAISE`` is for positive feedback.

    See ``code_review_pr`` skill rubric in ``prompts.py`` for the full
    definitions with examples.
    """

    CRITICAL = "critical"   # provable bug + security / auth / API contract impact
    HIGH = "high"           # provable bug + functional impact (crash, wrong behavior)
    MEDIUM = "medium"       # real bug + subtle / conditional trigger
    LOW = "low"             # edge-case bug, specific input triggers
    WARNING = "warning"     # deprecated → mapped to MEDIUM by _SEVERITY_MAP
    NIT = "nit"             # not a defect — style, minor improvement
    PRAISE = "praise"       # positive feedback


class FindingCategory(str, Enum):
    CORRECTNESS = "correctness"
    CONCURRENCY = "concurrency"
    SECURITY = "security"
    RELIABILITY = "reliability"
    PERFORMANCE = "performance"
    TEST_COVERAGE = "test_coverage"
    STYLE = "style"
    MAINTAINABILITY = "maintainability"


@dataclass
class ReviewFinding:
    """A single structured issue found during code review.

    Each finding is produced by a specific agent and includes
    evidence, location, and a suggested fix.
    """

    title: str
    category: FindingCategory
    severity: Severity
    confidence: float = 0.8  # 0.0–1.0
    file: str = ""
    start_line: int = 0
    end_line: int = 0
    evidence: List[str] = field(default_factory=list)
    risk: str = ""  # human-readable risk explanation
    suggested_fix: str = ""
    agent: str = ""  # which agent produced this
    reasoning: str = ""  # full chain-of-thought why this finding is valid
    rewrite_guidance: str = ""  # verifier-suggested wording improvements

    def score(self) -> float:
        """Compute a composite score for ranking."""
        severity_weight = {
            Severity.CRITICAL: 1.0,
            Severity.WARNING: 0.6,
            Severity.NIT: 0.2,
            Severity.PRAISE: 0.0,
        }
        return severity_weight.get(self.severity, 0.3) * self.confidence


# ---------------------------------------------------------------------------
# Aggregated review result
# ---------------------------------------------------------------------------


@dataclass
class AgentReviewResult:
    """Output from a single review agent."""

    agent_name: str
    findings: List[ReviewFinding] = field(default_factory=list)
    summary: str = ""
    tokens_used: int = 0
    iterations: int = 0
    duration_ms: float = 0.0
    error: Optional[str] = None


@dataclass
class ReviewResult:
    """Aggregated result from the multi-agent code review."""

    diff_spec: str
    pr_summary: str = ""
    risk_profile: Optional[RiskProfile] = None
    findings: List[ReviewFinding] = field(default_factory=list)
    agent_results: List[AgentReviewResult] = field(default_factory=list)
    files_reviewed: List[str] = field(default_factory=list)
    total_tokens: int = 0
    total_iterations: int = 0
    total_duration_ms: float = 0.0
    merge_recommendation: str = ""  # "approve", "request_changes", "approve_with_followups"
    synthesis: str = ""  # final polished review from strong model
    error: Optional[str] = None

    @property
    def critical_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == Severity.CRITICAL)

    @property
    def warning_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == Severity.WARNING)
