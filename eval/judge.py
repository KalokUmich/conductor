"""LLM-as-Judge for qualitative evaluation of code review output.

Uses an AIProvider to evaluate review quality on 4 criteria (1-5 scale):
  1. Completeness:       Did the review find all planted bugs?
  2. Reasoning quality:  Is the analysis well-reasoned with evidence?
  3. Actionability:      Are the suggestions concrete and fixable?
  4. False positive quality: Are the non-bug findings legitimate?

Output uses structured <analysis> + <verdict> tags for reliable parsing.
"""

import re
from dataclasses import dataclass, field
from typing import Optional

import sys
from pathlib import Path

_BACKEND_DIR = str(Path(__file__).resolve().parent.parent / "backend")
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

from app.ai_provider.base import AIProvider  # noqa: E402


JUDGE_SYSTEM_PROMPT = """\
You are a code review quality judge. You evaluate how well an automated code \
reviewer identified bugs planted in open-source code.

You will receive:
1. A description of the planted bug (ground truth)
2. The reviewer's findings (what it actually reported)
3. The reviewer's synthesis/summary

Score each criterion on a 1-5 scale:
- 1: Completely missed / wrong
- 2: Partially identified but major gaps
- 3: Identified the core issue but with notable gaps
- 4: Good identification with minor gaps
- 5: Excellent — thorough, precise, well-reasoned

Respond with EXACTLY this format:

<analysis>
Your detailed analysis of the review quality, discussing each criterion.
</analysis>

<verdict>
completeness: <score>
reasoning_quality: <score>
actionability: <score>
false_positive_quality: <score>
</verdict>"""


JUDGE_PROMPT_TEMPLATE = """\
## Planted Bug (Ground Truth)
**Case:** {case_title}
**Description:** {case_description}
**Expected location:** {expected_file} lines {expected_lines}
**Expected severity:** {expected_severity}

## Reviewer's Findings
{findings_text}

## Reviewer's Synthesis
{synthesis}

Evaluate the reviewer's performance on this case."""


@dataclass
class JudgeVerdict:
    """Parsed verdict from the LLM judge."""
    completeness: int = 0
    reasoning_quality: int = 0
    actionability: int = 0
    false_positive_quality: int = 0
    analysis: str = ""
    raw_response: str = ""
    error: Optional[str] = None

    @property
    def average(self) -> float:
        scores = [
            self.completeness,
            self.reasoning_quality,
            self.actionability,
            self.false_positive_quality,
        ]
        valid = [s for s in scores if s > 0]
        return sum(valid) / len(valid) if valid else 0.0

    def to_dict(self) -> dict:
        return {
            "completeness": self.completeness,
            "reasoning_quality": self.reasoning_quality,
            "actionability": self.actionability,
            "false_positive_quality": self.false_positive_quality,
            "average": round(self.average, 2),
            "analysis": self.analysis,
            "error": self.error,
        }


def judge_case(
    provider: AIProvider,
    case_title: str,
    case_description: str,
    expected_findings: list,
    findings: list,
    synthesis: str,
    model: Optional[str] = None,
) -> JudgeVerdict:
    """Run LLM judge on a single case.

    Args:
        provider: AI provider with call_model().
        case_title: Human-readable case name.
        case_description: What the planted bug is.
        expected_findings: Ground truth expected findings.
        findings: Actual ReviewFinding objects from the review.
        synthesis: The reviewer's synthesis text.
        model: Optional model override.

    Returns:
        JudgeVerdict with scores and analysis.
    """
    # Format expected finding info
    if expected_findings:
        exp = expected_findings[0]  # primary expected finding
        expected_file = exp.get("file_pattern", "unknown")
        line_range = exp.get("line_range", [])
        expected_lines = f"{line_range[0]}-{line_range[1]}" if len(line_range) == 2 else "unknown"
        expected_severity = exp.get("severity", "unknown")
    else:
        expected_file = "unknown"
        expected_lines = "unknown"
        expected_severity = "unknown"

    # Format actual findings
    findings_lines = []
    for i, f in enumerate(findings, 1):
        findings_lines.append(
            f"{i}. **{f.title}** [{f.severity.value}] "
            f"({f.category.value}) — {f.file}:{f.start_line}-{f.end_line}\n"
            f"   Evidence: {'; '.join(f.evidence[:3]) if f.evidence else 'none'}\n"
            f"   Fix: {f.suggested_fix[:200] if f.suggested_fix else 'none'}"
        )
    findings_text = "\n".join(findings_lines) if findings_lines else "(No findings reported)"

    prompt = JUDGE_PROMPT_TEMPLATE.format(
        case_title=case_title,
        case_description=case_description,
        expected_file=expected_file,
        expected_lines=expected_lines,
        expected_severity=expected_severity,
        findings_text=findings_text,
        synthesis=synthesis or "(No synthesis)",
    )

    try:
        response = provider.call_model(
            prompt=prompt,
            max_tokens=1500,
            system=JUDGE_SYSTEM_PROMPT,
        )
        return _parse_verdict(response)
    except Exception as e:
        return JudgeVerdict(error=str(e), raw_response="")


def _parse_verdict(response: str) -> JudgeVerdict:
    """Parse the structured verdict from the LLM response."""
    verdict = JudgeVerdict(raw_response=response)

    # Extract analysis
    analysis_match = re.search(
        r"<analysis>(.*?)</analysis>", response, re.DOTALL
    )
    if analysis_match:
        verdict.analysis = analysis_match.group(1).strip()

    # Extract verdict scores
    verdict_match = re.search(
        r"<verdict>(.*?)</verdict>", response, re.DOTALL
    )
    if not verdict_match:
        verdict.error = "Could not parse verdict tags from response"
        return verdict

    verdict_text = verdict_match.group(1)

    for field_name in ["completeness", "reasoning_quality", "actionability", "false_positive_quality"]:
        pattern = rf"{field_name}:\s*(\d)"
        m = re.search(pattern, verdict_text)
        if m:
            setattr(verdict, field_name, int(m.group(1)))

    return verdict
