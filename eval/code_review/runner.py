"""Workspace setup and CodeReviewService execution for eval cases.

Creates a temporary git repo from a source directory, applies a patch,
commits it, and runs CodeReviewService.review() against the diff.
"""

import logging
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Add backend/ to sys.path so we can import from backend.
# File is at eval/code_review/runner.py → 3 parents → repo root → backend/
_BACKEND_DIR = str(Path(__file__).resolve().parent.parent.parent / "backend")
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
    # Optional per-case source_dir override (relative to eval/code_review/).
    # When set, the loader uses this instead of the repo-level source_dir
    # from repos.yaml. Used by Greptile-imported cases where each PR has
    # its own pre-materialized base branch snapshot.
    source_dir: Optional[str] = None
    # Optional original git refs (kept for traceability; the materializer
    # uses base_ref to extract the snapshot pointed at by source_dir).
    base_ref: Optional[str] = None
    head_ref: Optional[str] = None


@dataclass
class RunResult:
    """Result from running a single eval case."""
    case_id: str
    review_result: Optional[ReviewResult] = None
    workspace_path: str = ""
    error: Optional[str] = None


# Directories to SKIP when copying the source tree into the eval workspace.
#
# IMPORTANT: do NOT exclude source-code directories (static/, tests/, public/)
# even though they're large. Review agents use grep / find_references /
# get_callers across the FULL workspace to trace cross-file impact — excluding
# legitimate source dirs would cause the reviewer to miss frontend breakage
# from backend changes, which is exactly the kind of bug we're testing for.
#
# Only exclude directories that are NEVER reviewed:
#   - Build artifacts / dependency caches (shouldn't appear in git-archive
#     output, but safety net)
#   - IDE / CI metadata
#   - Generated documentation
_WORKSPACE_EXCLUDE_DIRS: set = {
    # Build artifacts / dependency caches
    "node_modules",
    "__pycache__",
    "dist",
    "build",
    "target",
    "vendor",
    ".venv",
    "venv",
    # IDE / CI / docs metadata
    ".github",
    ".cursor",
    ".vscode",
    ".idea",
    "api-docs",
}


def setup_workspace(source_dir: str, patch_path: str, tmp_dir: Optional[str] = None) -> str:
    """Create a temp git repo from source, apply patch, and commit.

    Large eval repos (sentry ~17K files, grafana ~14K) would make the
    agents' grep / find_symbol calls catastrophically slow if copied in
    full. We skip directories in ``_WORKSPACE_EXCLUDE_DIRS`` — see the
    comment above for the rationale. The patch is applied with
    ``--allow-empty`` so that hunks touching excluded dirs are silently
    dropped (the agent still sees the hunks in ``git diff`` because the
    runner supplies the diff spec, not the workspace tree).

    Args:
        source_dir: Path to the plain source directory (no .git).
        patch_path: Path to the .patch file to apply.
        tmp_dir: Optional base directory for the temp workspace.

    Returns:
        Path to the temporary workspace with the patched commit.
    """
    workspace = tempfile.mkdtemp(dir=tmp_dir, prefix="eval_ws_")

    # Copy source tree — skip metadata-only dirs, hardlink files for speed.
    #
    # Large eval repos (sentry ~17K files) make shutil.copytree prohibitively
    # slow (~90s). Hardlinks are instant and safe: git-apply on a hardlinked
    # file triggers copy-on-write at the filesystem level, so the materialized
    # base stays intact.  We fall back to regular copy on filesystems that
    # don't support cross-directory hardlinks (e.g. different mount points).
    src = Path(source_dir)
    dst = Path(workspace)
    skipped = []
    hardlinked = 0
    copied = 0

    def _link_or_copy(s: Path, d: Path):
        nonlocal hardlinked, copied
        try:
            os.link(str(s), str(d))
            hardlinked += 1
        except OSError:
            shutil.copy2(str(s), str(d))
            copied += 1

    for item in src.iterdir():
        if item.name in _WORKSPACE_EXCLUDE_DIRS:
            skipped.append(item.name)
            continue
        if item.is_dir():
            shutil.copytree(
                str(item), str(dst / item.name),
                copy_function=lambda s, d: _link_or_copy(Path(s), Path(d)),
            )
        else:
            _link_or_copy(item, dst / item.name)

    if skipped or hardlinked:
        logger.info(
            "  setup_workspace: %d hardlinked, %d copied, %d dirs skipped (%s)",
            hardlinked, copied, len(skipped),
            ", ".join(sorted(skipped)) if skipped else "none",
        )

    # Initialize git repo
    _run_git(workspace, "init")
    _run_git(workspace, "config", "user.email", "eval@conductor.dev")
    _run_git(workspace, "config", "user.name", "Conductor Eval")
    _run_git(workspace, "add", "-A")
    _run_git(workspace, "commit", "-m", "Initial: clean source")

    # Apply patch — use --reject so hunks targeting excluded directories
    # are dropped rather than failing the whole apply. Any remaining .rej
    # files are cleaned up silently.
    try:
        _run_git(workspace, "apply", "--reject", patch_path)
    except subprocess.CalledProcessError:
        # Some hunks may have been rejected (expected when dirs are excluded).
        # Log but don't fail — the important hunks for the reviewed source
        # code will have applied successfully.
        rej_files = list(Path(workspace).rglob("*.rej"))
        if rej_files:
            logger.info("  setup_workspace: %d rejected hunks (excluded dirs)", len(rej_files))
            for rf in rej_files:
                rf.unlink()
        else:
            raise  # genuine failure, re-raise
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
    """Set up workspace, run PR Brain review via PRBrainOrchestrator, and return results.

    This is the Brain-pipeline equivalent of ``run_case()``.  It uses
    ``PRBrainOrchestrator`` instead of ``CodeReviewService`` so that the two
    pipelines can be compared directly on the same eval cases.

    Args:
        case: Case configuration with patch path and expected findings.
        source_dir: Path to the repo source directory.
        patch_dir: Directory containing patch files.
        provider: Strong AI provider used for synthesis (Brain's LLM).
        explorer_provider: Optional lighter model for review sub-agents;
            falls back to ``provider`` if not supplied.

    Returns:
        RunResult with the review output or error.
    """
    patch_path = os.path.join(patch_dir, case.patch)
    if not os.path.exists(patch_path):
        return RunResult(case_id=case.id, error=f"Patch not found: {patch_path}")

    workspace = None
    try:
        workspace = setup_workspace(source_dir, patch_path)

        from app.agent_loop.pr_brain import PRBrainOrchestrator
        from app.code_tools.executor import LocalToolExecutor
        from app.workflow.loader import load_agent_registry, load_pr_brain_config

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
            task_id=f"eval-{case.id}",
            pr_title=case.title or "",
            pr_description=case.description or "",
        )

        # Collect events from the pipeline
        findings = []
        synthesis = ""
        merge_rec = ""
        files_reviewed = []
        total_tokens = 0

        try:
            async for event in orchestrator.run_stream():
                if event.kind == "done":
                    data = event.data
                    synthesis = data.get("answer", "")
                    findings_data = data.get("findings", [])
                    merge_rec = data.get("merge_recommendation", "")
                    files_reviewed = data.get("files_reviewed", [])

                    # Convert finding dicts back to ReviewFinding objects
                    from app.code_review.models import FindingCategory, ReviewFinding, Severity
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
        finally:
            # Phase 9.15 — release the per-case Fact Vault. Without this,
            # each case leaks ~40KB into ~/.conductor/scratchpad/ and the
            # cache_perf stats never log, robbing us of validation data.
            orchestrator.cleanup()

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
    """Run a git command in the given directory.

    Args:
        cwd: Working directory for the git command.
        *args: Git sub-command and arguments (e.g. ``"add"``, ``"-A"``).

    Returns:
        Captured stdout from the git process.

    Raises:
        RuntimeError: If the git command exits with a non-zero return code.
    """
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
