"""Workspace setup and CodeReviewService execution for eval cases.

Creates a temporary git repo from a source directory, applies a patch,
commits it, and runs CodeReviewService.review() against the diff.
"""

import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# Add backend/ to sys.path so we can import from backend
_BACKEND_DIR = str(Path(__file__).resolve().parent.parent / "backend")
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

from app.ai_provider.base import AIProvider  # noqa: E402
from app.code_review.models import ReviewResult  # noqa: E402
from app.code_review.service import CodeReviewService  # noqa: E402


@dataclass
class CaseConfig:
    """Parsed case definition from cases.yaml."""
    id: str
    patch: str  # relative path to patch file
    difficulty: str
    title: str
    description: str
    expected_findings: list = field(default_factory=list)


@dataclass
class RunResult:
    """Result from running a single eval case."""
    case_id: str
    review_result: Optional[ReviewResult] = None
    workspace_path: str = ""
    error: Optional[str] = None


def setup_workspace(source_dir: str, patch_path: str, tmp_dir: Optional[str] = None) -> str:
    """Create a temp git repo from source, apply patch, and commit.

    Args:
        source_dir: Path to the plain source directory (no .git).
        patch_path: Path to the .patch file to apply.
        tmp_dir: Optional base directory for the temp workspace.

    Returns:
        Path to the temporary workspace with the patched commit.
    """
    workspace = tempfile.mkdtemp(dir=tmp_dir, prefix="eval_ws_")

    # Copy source tree
    src = Path(source_dir)
    dst = Path(workspace)
    for item in src.iterdir():
        s = str(item)
        d = str(dst / item.name)
        if item.is_dir():
            shutil.copytree(s, d)
        else:
            shutil.copy2(s, d)

    # Initialize git repo
    _run_git(workspace, "init")
    _run_git(workspace, "config", "user.email", "eval@conductor.dev")
    _run_git(workspace, "config", "user.name", "Conductor Eval")
    _run_git(workspace, "add", "-A")
    _run_git(workspace, "commit", "-m", "Initial: clean source")

    # Apply patch
    _run_git(workspace, "apply", patch_path)
    _run_git(workspace, "add", "-A")
    _run_git(workspace, "commit", "-m", "Apply bug patch")

    return workspace


def cleanup_workspace(workspace_path: str) -> None:
    """Remove a temporary workspace directory."""
    shutil.rmtree(workspace_path, ignore_errors=True)


async def run_case(
    case: CaseConfig,
    source_dir: str,
    patch_dir: str,
    provider: AIProvider,
    explorer_provider: Optional[AIProvider] = None,
    max_agents: int = 5,
) -> RunResult:
    """Set up workspace, run code review, and return results.

    Args:
        case: Case configuration with patch path and expected findings.
        source_dir: Path to the repo source directory.
        patch_dir: Directory containing patch files.
        provider: Main AI provider (strong model for synthesis).
        explorer_provider: Optional lighter model for sub-agents.
        max_agents: Maximum parallel agents for the review.

    Returns:
        RunResult with the review output or error.
    """
    patch_path = os.path.join(patch_dir, case.patch)
    if not os.path.exists(patch_path):
        return RunResult(case_id=case.id, error=f"Patch not found: {patch_path}")

    workspace = None
    try:
        workspace = setup_workspace(source_dir, patch_path)

        service = CodeReviewService(
            provider=provider,
            explorer_provider=explorer_provider,
        )

        result = await service.review(
            workspace_path=workspace,
            diff_spec="HEAD~1..HEAD",
            max_agents=max_agents,
        )

        return RunResult(
            case_id=case.id,
            review_result=result,
            workspace_path=workspace,
        )

    except Exception as e:
        return RunResult(case_id=case.id, error=str(e))
    finally:
        if workspace:
            cleanup_workspace(workspace)


async def run_case_brain(
    case: CaseConfig,
    source_dir: str,
    patch_dir: str,
    provider: AIProvider,
    explorer_provider: Optional[AIProvider] = None,
) -> RunResult:
    """Set up workspace, run PR Brain review, and return results.

    Uses the new PRBrainOrchestrator instead of CodeReviewService.
    """
    patch_path = os.path.join(patch_dir, case.patch)
    if not os.path.exists(patch_path):
        return RunResult(case_id=case.id, error=f"Patch not found: {patch_path}")

    workspace = None
    try:
        workspace = setup_workspace(source_dir, patch_path)

        from app.agent_loop.pr_brain import PRBrainOrchestrator
        from app.workflow.loader import load_pr_brain_config, load_agent_registry
        from app.code_tools.executor import LocalToolExecutor

        pr_brain_config = load_pr_brain_config()
        agent_registry = load_agent_registry()
        tool_executor = LocalToolExecutor(workspace)

        orchestrator = PRBrainOrchestrator(
            provider=provider,
            explorer_provider=explorer_provider or provider,
            workspace_path=workspace,
            diff_spec="HEAD~1..HEAD",
            pr_brain_config=pr_brain_config,
            agent_registry=agent_registry,
            tool_executor=tool_executor,
        )

        # Collect events from the pipeline
        findings = []
        synthesis = ""
        merge_rec = ""
        files_reviewed = []
        total_tokens = 0

        async for event in orchestrator.run_stream():
            if event.kind == "done":
                data = event.data
                synthesis = data.get("answer", "")
                findings_data = data.get("findings", [])
                merge_rec = data.get("merge_recommendation", "")
                files_reviewed = data.get("files_reviewed", [])

                # Convert finding dicts back to ReviewFinding objects
                from app.code_review.models import ReviewFinding, Severity, FindingCategory
                for fd in findings_data:
                    try:
                        findings.append(ReviewFinding(
                            title=fd.get("title", ""),
                            category=FindingCategory(fd.get("category", "correctness")),
                            severity=Severity(fd.get("severity", "warning")),
                            confidence=fd.get("confidence", 0.7),
                            file=fd.get("file", ""),
                            start_line=fd.get("start_line", 0),
                            end_line=fd.get("end_line", 0),
                            evidence=fd.get("evidence", []),
                            risk=fd.get("risk", ""),
                            suggested_fix=fd.get("suggested_fix", ""),
                            agent=fd.get("agent", ""),
                        ))
                    except (ValueError, KeyError):
                        continue

        review_result = ReviewResult(
            diff_spec="HEAD~1..HEAD",
            findings=findings,
            files_reviewed=files_reviewed,
            synthesis=synthesis,
            merge_recommendation=merge_rec,
        )

        return RunResult(
            case_id=case.id,
            review_result=review_result,
            workspace_path=workspace,
        )

    except Exception as e:
        import traceback
        traceback.print_exc()
        return RunResult(case_id=case.id, error=str(e))
    finally:
        if workspace:
            cleanup_workspace(workspace)


def _run_git(cwd: str, *args: str) -> str:
    """Run a git command in the given directory."""
    result = subprocess.run(
        ["git"] + list(args),
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed (rc={result.returncode}): {result.stderr}"
        )
    return result.stdout
