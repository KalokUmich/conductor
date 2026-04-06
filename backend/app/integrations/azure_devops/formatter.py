"""Format ReviewFinding → Azure DevOps PR thread Markdown.

Each finding is split into per-location inline threads (one per evidence
line reference) following the same pattern as Google Gemini Code Review
and CodeRabbit: short, focused inline comments at each affected line,
with a PR-level summary tying everything together.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional

from app.code_review.models import ReviewFinding, ReviewResult, Severity

# ---------------------------------------------------------------------------
# Severity → visual badge
# ---------------------------------------------------------------------------

_SEVERITY_BADGE = {
    Severity.CRITICAL: "\U0001f534 **Critical**",
    Severity.WARNING: "\U0001f7e0 **Warning**",
    Severity.NIT: "\U0001f535 **Suggestion**",
    Severity.PRAISE: "\U0001f7e2 **Nice**",
}

_SEVERITY_EMOJI = {
    Severity.CRITICAL: "\U0001f534",
    Severity.WARNING: "\U0001f7e0",
    Severity.NIT: "\U0001f535",
    Severity.PRAISE: "\U0001f7e2",
}

# Regex to extract "Line NNN:" or "Lines NNN-MMM:" from evidence strings
_LINE_RE = re.compile(r"^Lines?\s+(\d+)(?:\s*[-–]\s*(\d+))?:\s*(.+)", re.IGNORECASE)


@dataclass
class InlineComment:
    """A single inline comment to post on the PR."""

    file_path: Optional[str]
    start_line: Optional[int]
    end_line: Optional[int]
    content: str


def _extract_evidence_locations(
    finding: ReviewFinding,
) -> List[tuple]:
    """Extract (line, end_line, evidence_text) from evidence strings.

    Returns a list of (start_line, end_line, evidence_text) for evidence
    items that reference specific line numbers.
    """
    locations: List[tuple] = []
    for e in finding.evidence:
        m = _LINE_RE.match(e)
        if m:
            start = int(m.group(1))
            end = int(m.group(2)) if m.group(2) else start
            locations.append((start, end, m.group(3).strip()))
    return locations


def split_finding_into_comments(finding: ReviewFinding) -> List[InlineComment]:
    """Split a finding into per-location inline comments.

    Strategy (following Google/CodeRabbit pattern):
    - Extract all line-referenced evidence items
    - Group by proximity (lines within 5 of each other → same comment)
    - Primary comment gets title + risk + suggested fix
    - Secondary comments are brief with cross-reference
    - If no line references found, fall back to single comment at finding.start_line
    """
    badge = _SEVERITY_BADGE.get(finding.severity, "\u26a0\ufe0f **Issue**")
    locations = _extract_evidence_locations(finding)

    if not locations:
        # No line references — single comment at the finding's location
        return [InlineComment(
            file_path=finding.file or None,
            start_line=finding.start_line or None,
            end_line=finding.start_line or None,
            content=_format_primary_comment(finding, badge),
        )]

    # Group nearby lines (within 20 lines → same comment, same diff hunk)
    groups: List[List[tuple]] = []
    for loc in sorted(locations, key=lambda x: x[0]):
        if groups and loc[0] - groups[-1][-1][1] <= 20:
            groups[-1].append(loc)
        else:
            groups.append([loc])

    comments: List[InlineComment] = []
    other_lines = [g[0][0] for g in groups]

    for i, group in enumerate(groups):
        start = group[0][0]
        end = group[-1][1]
        evidence_texts = [loc[2] for loc in group]

        if i == 0:
            # Primary comment — full detail
            also = ""
            if len(other_lines) > 1:
                others = [str(ln) for ln in other_lines[1:]]
                also = f"\n\n_Also at: line {', '.join(others)}_"

            content = (
                f"{badge}\n\n"
                f"**{finding.title}**\n\n"
            )
            if finding.risk:
                content += f"{finding.risk}\n\n"
            for et in evidence_texts:
                content += f"- {et}\n"
            if finding.suggested_fix:
                content += f"\n**Suggested fix:**\n```\n{finding.suggested_fix}\n```\n"
            content += also
        else:
            # Secondary comment — brief, cross-reference primary
            content = (
                f"{badge} Same pattern as line {other_lines[0]}\n\n"
            )
            for et in evidence_texts:
                content += f"- {et}\n"

        comments.append(InlineComment(
            file_path=finding.file or None,
            start_line=start,
            end_line=min(end, start + 10),  # Keep highlight focused
            content=content.strip(),
        ))

    return comments


def _format_primary_comment(finding: ReviewFinding, badge: str) -> str:
    """Format a single-location finding (no line refs in evidence)."""
    lines = [f"{badge} (confidence: {finding.confidence:.0%})"]
    lines.append("")
    lines.append(f"**{finding.title}**")
    lines.append("")

    if finding.risk:
        lines.append(finding.risk)
        lines.append("")

    if finding.evidence:
        for e in finding.evidence:
            lines.append(f"- {e}")
        lines.append("")

    if finding.suggested_fix:
        lines.append("**Suggested fix:**")
        lines.append("```")
        lines.append(finding.suggested_fix)
        lines.append("```")

    return "\n".join(lines)


def format_summary_markdown(result: ReviewResult) -> str:
    """Format the overall review summary as a PR-level comment."""

    lines = []
    lines.append("## \U0001f916 Conductor AI Code Review")
    lines.append("")

    # Stats
    lines.append(
        f"Reviewed **{len(result.files_reviewed)}** files | "
        f"Found **{len(result.findings)}** issues "
        f"({result.critical_count} critical, {result.warning_count} warning)"
    )
    lines.append("")

    # Recommendation
    rec = result.merge_recommendation or "no_recommendation"
    rec_display = {
        "approve": "\u2705 **Approve**",
        "approve_with_followups": "\u2705 **Approve** (with follow-ups)",
        "request_changes": "\u274c **Request Changes**",
    }.get(rec, f"\u2753 {rec}")
    lines.append(f"**Recommendation:** {rec_display}")
    lines.append("")

    # PR summary (e.g. rejection reason for oversized PRs)
    if result.pr_summary:
        lines.append(result.pr_summary)
        lines.append("")

    # Findings summary table
    if result.findings:
        lines.append("### Findings")
        lines.append("")
        lines.append("| Severity | File | Title |")
        lines.append("|----------|------|-------|")
        for f in result.findings:
            emoji = _SEVERITY_EMOJI.get(f.severity, "\u26a0\ufe0f")
            loc = f"`{f.file}:{f.start_line}`" if f.file else "-"
            lines.append(f"| {emoji} {f.severity.value} | {loc} | {f.title} |")
        lines.append("")

    # Synthesis (if available)
    if result.synthesis:
        lines.append("### Detailed Analysis")
        lines.append("")
        lines.append(result.synthesis)
        lines.append("")

    # Agent stats
    lines.append("<details>")
    lines.append("<summary>Agent Statistics</summary>")
    lines.append("")
    lines.append(f"- Total tokens: {result.total_tokens:,}")
    lines.append(f"- Total iterations: {result.total_iterations}")
    lines.append(f"- Duration: {result.total_duration_ms / 1000:.1f}s")
    for ar in result.agent_results:
        lines.append(f"- **{ar.agent_name}**: {ar.iterations} iterations, {ar.tokens_used:,} tokens")
    lines.append("")
    lines.append("</details>")

    return "\n".join(lines)


def recommendation_to_vote(recommendation: str) -> int:
    """Map merge_recommendation to Azure DevOps vote value.

    Azure DevOps vote values:
        10  = approved
         5  = approved with suggestions
         0  = no vote
        -5  = waiting for author
       -10  = rejected
    """
    return {
        "approve": 10,
        "approve_with_followups": 5,
        "request_changes": -5,
    }.get(recommendation, 0)
