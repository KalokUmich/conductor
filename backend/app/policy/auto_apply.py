"""Auto-apply policy evaluation for code changes.

This module implements safety checks that determine whether a ChangeSet
can be automatically applied without explicit user confirmation.

The auto-apply feature is designed for:
    - Small, low-risk changes (few files, few lines)
    - Non-critical code paths (not infra, db, or security)
    - Lead users only (member role cannot use auto-apply)

Policy Rules:
    1. max_files <= 2: Prevents large-scale automated changes
    2. max_lines_changed <= 50: Limits blast radius of each change
    3. forbidden_paths: Blocks changes to critical infrastructure

Security Rationale:
    Auto-apply trades convenience for safety. By limiting scope and
    excluding critical paths, we reduce the risk of accidental damage
    while still allowing quick iteration on routine changes.

Future Enhancements:
    - Per-project policy overrides
    - ML-based risk assessment
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, List, Tuple

from app.agent.schemas import ChangeSet, ChangeType

if TYPE_CHECKING:
    from app.config import ConductorConfig


# =============================================================================
# Policy Constants (TODO: Move to configuration file)
# =============================================================================

# Maximum number of files that can be auto-applied
MAX_FILES = 2

# Maximum total lines changed across all files
MAX_LINES_CHANGED = 50

# Path prefixes that are never auto-applied (critical infrastructure)
FORBIDDEN_PATHS: Tuple[str, ...] = ("infra/", "db/", "security/")


@dataclass
class PolicyResult:
    """Result of a policy evaluation.
    
    Attributes:
        allowed: Whether the auto-apply is allowed
        reasons: List of reasons why the policy failed (empty if allowed)
    """
    allowed: bool
    reasons: List[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        """Allow using PolicyResult in boolean context."""
        return self.allowed


class AutoApplyPolicy:
    """Policy evaluator for Auto Apply feature.
    
    This class evaluates whether a ChangeSet can be auto-applied
    based on hard-coded rules:
    - max_files <= 2
    - max_lines_changed <= 50
    - forbidden paths: infra/, db/, security/
    """

    def __init__(
        self,
        max_files: int = MAX_FILES,
        max_lines_changed: int = MAX_LINES_CHANGED,
        forbidden_paths: tuple = FORBIDDEN_PATHS,
    ):
        """Initialize the policy with configurable limits.
        
        Args:
            max_files: Maximum number of files allowed (default: 2)
            max_lines_changed: Maximum total lines changed (default: 50)
            forbidden_paths: Tuple of forbidden path prefixes (default: infra/, db/, security/)
        """
        self.max_files = max_files
        self.max_lines_changed = max_lines_changed
        self.forbidden_paths = forbidden_paths

    def evaluate(self, change_set: ChangeSet) -> PolicyResult:
        """Evaluate whether a ChangeSet passes the auto-apply policy.
        
        Args:
            change_set: The ChangeSet to evaluate
            
        Returns:
            PolicyResult with allowed=True if all rules pass,
            or allowed=False with reasons if any rule fails.
        """
        reasons: List[str] = []

        # Rule 1: Check max files
        num_files = len(change_set.changes)
        if num_files > self.max_files:
            reasons.append(
                f"Too many files: {num_files} > {self.max_files}"
            )

        # Rule 2: Check max lines changed
        total_lines = self._count_lines_changed(change_set)
        if total_lines > self.max_lines_changed:
            reasons.append(
                f"Too many lines changed: {total_lines} > {self.max_lines_changed}"
            )

        # Rule 3: Check forbidden paths
        forbidden_files = self._find_forbidden_files(change_set)
        if forbidden_files:
            reasons.append(
                f"Forbidden paths: {', '.join(forbidden_files)}"
            )

        return PolicyResult(
            allowed=len(reasons) == 0,
            reasons=reasons
        )

    def _count_lines_changed(self, change_set: ChangeSet) -> int:
        """Count the total number of lines changed in a ChangeSet.
        
        For replace_range: counts lines in the range (end - start + 1)
        For create_file: counts lines in the content
        
        Args:
            change_set: The ChangeSet to count lines for
            
        Returns:
            Total number of lines changed
        """
        total = 0
        for change in change_set.changes:
            if change.type == ChangeType.REPLACE_RANGE and change.range:
                # Count lines in the range being replaced
                total += change.range.end - change.range.start + 1
            elif change.type == ChangeType.CREATE_FILE and change.content:
                # Count lines in the new file content
                total += change.content.count('\n') + 1
        return total

    def _find_forbidden_files(self, change_set: ChangeSet) -> List[str]:
        """Find files that match forbidden path prefixes.
        
        Args:
            change_set: The ChangeSet to check
            
        Returns:
            List of file paths that match forbidden prefixes
        """
        forbidden = []
        for change in change_set.changes:
            file_path = change.file
            for prefix in self.forbidden_paths:
                if file_path.startswith(prefix):
                    forbidden.append(file_path)
                    break
        return forbidden


# Singleton instance for convenience
_default_policy = AutoApplyPolicy()


def evaluate_auto_apply(
    change_set: ChangeSet,
    config: ConductorConfig | None = None,
) -> PolicyResult:
    """Evaluate a ChangeSet against the auto-apply policy.

    When *config* is provided the limits are read from
    ``config.change_limits``; otherwise the module-level defaults are used.

    Args:
        change_set: The ChangeSet to evaluate
        config: Optional ConductorConfig for reading limits from settings

    Returns:
        PolicyResult indicating whether auto-apply is allowed
    """
    if config is not None:
        limits = config.change_limits
        policy = AutoApplyPolicy(
            max_files=limits.max_files_per_request,
            max_lines_changed=limits.auto_apply.max_lines,
        )
        return policy.evaluate(change_set)
    return _default_policy.evaluate(change_set)

