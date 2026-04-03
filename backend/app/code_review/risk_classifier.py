"""Risk classification layer — analyses PR context to produce a RiskProfile.

Uses rule-based heuristics on file paths, change patterns, and file categories
to score risk across 5 dimensions: correctness, concurrency, security,
reliability, and operational.

The risk profile drives agent dispatch — high-risk dimensions get specialized
agents with larger budgets.
"""

from __future__ import annotations

import re
from typing import List

from .models import ChangedFile, FileCategory, PRContext, RiskLevel, RiskProfile

# ---------------------------------------------------------------------------
# Pattern sets for risk detection
# ---------------------------------------------------------------------------

_CONCURRENCY_PATTERNS = [
    re.compile(r"(?i)consumer|listener|handler|callback|webhook"),
    re.compile(r"(?i)queue|mq|kafka|sqs|rabbit|amqp|pubsub"),
    re.compile(r"(?i)async|await|thread|lock|mutex|semaphore"),
    re.compile(r"(?i)retry|backoff|idempoten"),
    re.compile(r"(?i)scheduled|cron|timer|interval"),
    re.compile(r"(?i)worker|job|task|celery"),
]

_SECURITY_PATTERNS = [
    re.compile(r"(?i)auth|login|logout|session|token|jwt|oauth|sso"),
    re.compile(r"(?i)password|secret|credential|api.?key"),
    re.compile(r"(?i)permission|rbac|acl|role|access.?control"),
    re.compile(r"(?i)encrypt|decrypt|hash|hmac|sign|verify"),
    re.compile(r"(?i)sanitiz|escap|xss|csrf|inject|sql"),
    re.compile(r"(?i)cors|header|cookie|origin"),
]

_RELIABILITY_PATTERNS = [
    re.compile(r"(?i)exception|error|catch|throw|raise"),
    re.compile(r"(?i)timeout|deadline|circuit.?breaker"),
    re.compile(r"(?i)fallback|recover|graceful|shutdown"),
    re.compile(r"(?i)health.?check|readiness|liveness"),
    re.compile(r"(?i)metric|monitor|alert|observ"),
    re.compile(r"(?i)log(ger|ging)?\."),
]

_CORRECTNESS_PATTERNS = [
    re.compile(r"(?i)state.?machine|status|transition|workflow"),
    re.compile(r"(?i)persist|save|update|delete|insert|upsert"),
    re.compile(r"(?i)transaction|commit|rollback"),
    re.compile(r"(?i)validat|assert|check|verify|constrain"),
    re.compile(r"(?i)schema|migration|alter.?table"),
]

_OPERATIONAL_PATTERNS = [
    re.compile(r"(?i)\.ya?ml$|\.env|\.properties|\.toml"),
    re.compile(r"(?i)docker|kubernetes|helm|terraform"),
    re.compile(r"(?i)deploy|release|rollout|canary"),
    re.compile(r"(?i)feature.?flag|toggle|config"),
    re.compile(r"(?i)pool|connection|cache|redis|memcache"),
]


def _count_matches(files: List[ChangedFile], patterns: list) -> int:
    """Count how many files match any pattern."""
    count = 0
    for f in files:
        for pat in patterns:
            if pat.search(f.path):
                count += 1
                break
    return count


def _level_from_count(count: int, total_files: int) -> RiskLevel:
    """Convert a match count to a risk level."""
    if count == 0:
        return RiskLevel.LOW
    ratio = count / max(total_files, 1)
    if count >= 5 or ratio > 0.3:
        return RiskLevel.HIGH
    if count >= 2 or ratio > 0.15:
        return RiskLevel.MEDIUM
    return RiskLevel.LOW


def classify_risk(pr_context: PRContext) -> RiskProfile:
    """Classify the risk profile of a PR based on changed files.

    Args:
        pr_context: Parsed PR context with classified files.

    Returns:
        RiskProfile with risk levels across 5 dimensions.
    """
    files = pr_context.files
    total = len(files)

    # Base risk from file count and change size
    correctness_count = _count_matches(files, _CORRECTNESS_PATTERNS)
    concurrency_count = _count_matches(files, _CONCURRENCY_PATTERNS)
    security_count = _count_matches(files, _SECURITY_PATTERNS)
    reliability_count = _count_matches(files, _RELIABILITY_PATTERNS)
    operational_count = _count_matches(files, _OPERATIONAL_PATTERNS)

    profile = RiskProfile(
        correctness=_level_from_count(correctness_count, total),
        concurrency=_level_from_count(concurrency_count, total),
        security=_level_from_count(security_count, total),
        reliability=_level_from_count(reliability_count, total),
        operational=_level_from_count(operational_count, total),
    )

    # Boost correctness risk for large PRs touching business logic
    biz_files = pr_context.business_logic_files()
    if (len(biz_files) >= 10 or pr_context.total_changed_lines > 2000) and profile.correctness == RiskLevel.LOW:
        profile.correctness = RiskLevel.MEDIUM

    # Boost correctness for schema/migration changes
    schema_count = sum(1 for f in files if f.category == FileCategory.SCHEMA)
    if schema_count > 0 and profile.correctness.value < RiskLevel.MEDIUM.value:
        profile.correctness = RiskLevel.MEDIUM

    # Config changes boost operational risk
    config_count = sum(1 for f in files if f.category == FileCategory.CONFIG)
    if config_count >= 3 and profile.operational == RiskLevel.LOW:
        profile.operational = RiskLevel.MEDIUM

    return profile
