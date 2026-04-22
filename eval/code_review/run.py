#!/usr/bin/env python3
"""CLI entrypoint for the code review eval system.

Two modes:

  Brain mode (default, pass ``--brain``):
    Runs ``PRBrainOrchestrator`` (coordinator-worker v2 loop) against the
    eval cases. This is the only production pipeline.
    python eval/run.py --brain --provider bedrock --model <sonnet-id>

  Gold-standard mode (--gold):
    Invokes Claude Code CLI directly (own tools, own strategies).
    Produces the quality ceiling baseline for comparison.
    python eval/run.py --gold --save-baseline
    python eval/run.py --gold --gold-model opus --filter "requests-001"

Examples:
    python eval/run.py --brain --filter "requests-001"
    python eval/run.py --brain --no-judge
    python eval/run.py --gold --save-baseline
    python eval/run.py --compare-gold
"""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Optional

import yaml

# Ensure eval/ is on sys.path for local imports
_EVAL_DIR = Path(__file__).resolve().parent
if str(_EVAL_DIR) not in sys.path:
    sys.path.insert(0, str(_EVAL_DIR))

# Backend imports (runner adds backend/ to sys.path)
from runner import CaseConfig, RunResult, run_case_brain  # noqa: E402
from gold_runner import GoldRunResult, run_gold_case  # noqa: E402
from scorer import CaseScore, score_case  # noqa: E402
from judge import judge_case  # noqa: E402
from report import (  # noqa: E402
    EvalReport, build_report, print_report,
    save_baseline, save_gold_baseline, load_latest_gold_baseline,
)

# Directory for saving full gold traces (alongside gold_baselines/)
_GOLD_TRACES_DIR = Path(__file__).resolve().parent / "gold_traces"


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments for the code review eval CLI.

    Returns:
        Parsed argparse namespace with all eval configuration options.
    """
    parser = argparse.ArgumentParser(
        description="Run code review eval suite",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--filter", type=str, default=None,
        help="Run only cases matching this substring (e.g. 'requests-001')",
    )
    parser.add_argument(
        "--no-judge", action="store_true",
        help="Skip LLM judge evaluation (deterministic scoring only)",
    )
    parser.add_argument(
        "--save-baseline", action="store_true",
        help="Save results as a new baseline after the run",
    )
    parser.add_argument(
        "--provider", type=str, default="anthropic",
        choices=["anthropic", "bedrock", "openai"],
        help="AI provider to use (default: anthropic)",
    )
    parser.add_argument(
        "--model", type=str, default=None,
        help="Model ID override (provider-specific)",
    )
    parser.add_argument(
        "--explorer-model", type=str, default=None,
        help="Model for sub-agents (lighter/faster model)",
    )
    parser.add_argument(
        "--parallelism", type=int, default=1,
        help="Number of cases to run in parallel (default: 1)",
    )
    parser.add_argument(
        "--max-agents", type=int, default=5,
        help="Max parallel agents per review (default: 5)",
    )
    parser.add_argument(
        "--brain", action="store_true", default=True,
        help="Run the PR Brain orchestrator (default; kept for backward-compat with existing invocations).",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Print per-finding details (title, severity, file:line, match result)",
    )

    # Gold-standard baseline options
    gold_group = parser.add_argument_group("gold-standard baseline")
    gold_group.add_argument(
        "--gold", action="store_true",
        help="Run in gold-standard mode (Claude Code CLI, own tools)",
    )
    gold_group.add_argument(
        "--gold-model", type=str, default="opus",
        help="Claude Code model alias for gold runs (default: opus)",
    )
    gold_group.add_argument(
        "--gold-max-budget", type=float, default=5.0,
        help="Max USD spend per gold case (default: 5.0)",
    )
    gold_group.add_argument(
        "--compare-gold", action="store_true",
        help="Compare pipeline results against the latest gold baseline",
    )

    return parser.parse_args()


def create_provider(provider_name: str, model: str = None):
    """Create an AIProvider instance from CLI provider/model arguments.

    Reads credentials from environment variables (ANTHROPIC_API_KEY,
    AWS_ACCESS_KEY_ID, OPENAI_API_KEY, etc.) and exits with an error
    message if required credentials are missing.

    Args:
        provider_name: One of ``"anthropic"``, ``"bedrock"``, or ``"openai"``.
        model: Optional model ID override; falls back to a sensible default
            for each provider.

    Returns:
        A configured AIProvider instance.

    Raises:
        SystemExit: If required API credentials are not set, or if
            ``provider_name`` is unrecognised.
    """
    _backend = str(Path(__file__).resolve().parent.parent / "backend")
    if _backend not in sys.path:
        sys.path.insert(0, _backend)

    if provider_name == "anthropic":
        from app.ai_provider.claude_direct import ClaudeDirectProvider
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            print("ERROR: ANTHROPIC_API_KEY not set", file=sys.stderr)
            sys.exit(1)
        return ClaudeDirectProvider(
            api_key=api_key,
            model=model or "claude-sonnet-4-20250514",
        )

    elif provider_name == "bedrock":
        from app.ai_provider.claude_bedrock import ClaudeBedrockProvider
        # Try environment variables first, fall back to conductor secrets yaml
        access_key = os.environ.get("AWS_ACCESS_KEY_ID", "")
        secret_key = os.environ.get("AWS_SECRET_ACCESS_KEY", "")
        session_token = os.environ.get("AWS_SESSION_TOKEN")
        region = os.environ.get("AWS_DEFAULT_REGION", "")
        if not access_key or not secret_key:
            try:
                from app.config import load_config
                cfg = load_config()
                bdr = cfg.ai_providers.aws_bedrock
                access_key = access_key or bdr.access_key_id
                secret_key = secret_key or bdr.secret_access_key
                session_token = session_token or bdr.session_token
                region = region or bdr.region
            except Exception:
                pass
        return ClaudeBedrockProvider(
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            aws_session_token=session_token,
            region_name=region or "us-east-1",
            model_id=model or "eu.anthropic.claude-sonnet-4-6",
        )

    elif provider_name == "openai":
        from app.ai_provider.openai_provider import OpenAIProvider
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            print("ERROR: OPENAI_API_KEY not set", file=sys.stderr)
            sys.exit(1)
        return OpenAIProvider(
            api_key=api_key,
            model=model or "gpt-4o",
        )

    else:
        print(f"ERROR: Unknown provider: {provider_name}", file=sys.stderr)
        sys.exit(1)


def load_cases(eval_dir: Path, filter_str: str = None) -> list:
    """Load all case configs from ``repos.yaml`` and per-repo ``cases.yaml`` files.

    Two case styles are supported:

    1. **Repo-level source_dir** (legacy): each entry in ``repos.yaml`` declares
       a single ``source_dir``; every case in that repo's ``cases.yaml`` shares
       it. This is how the original ``requests`` cases work.

    2. **Per-case source_dir** (Greptile-style): an individual case may set its
       own ``source_dir`` field, which overrides the repo-level one. The
       Greptile importer uses this because every PR in the benchmark has its
       own pre-materialized base-branch snapshot under
       ``repos/greptile_bases/{target}/{pr_num}/``.

    Cases dirs that have NO entry in ``repos.yaml`` are ALSO loaded — this
    is how greptile_{target} dirs are picked up without us having to keep
    repos.yaml in sync.

    Args:
        eval_dir: Path to the ``eval/code_review/`` directory.
        filter_str: Optional substring filter applied to case IDs.

    Returns:
        List of ``(CaseConfig, source_dir, patch_dir)`` tuples.
    """
    repos_path = eval_dir / "repos.yaml"
    with open(repos_path) as f:
        repos_config = yaml.safe_load(f)

    all_cases = []
    cases_root = eval_dir / "cases"

    # 1) Repos declared in repos.yaml — they get a default source_dir
    seen_dirs: set = set()
    for repo_name, repo_info in repos_config.get("repos", {}).items():
        cases_path = cases_root / repo_name / "cases.yaml"
        seen_dirs.add(repo_name)
        if not cases_path.exists():
            print(f"WARNING: No cases.yaml for repo '{repo_name}'", file=sys.stderr)
            continue

        default_source = str(eval_dir / repo_info["source_dir"])
        patch_dir = str(cases_root / repo_name)

        with open(cases_path) as f:
            cases_data = yaml.safe_load(f)
        all_cases.extend(_build_case_tuples(cases_data, default_source, patch_dir, eval_dir))

    # 2) Auto-discover any cases dir that doesn't have a repos.yaml entry
    #    (e.g. cases/greptile_sentry/). Each case MUST set its own source_dir.
    #    Each dir may also contain ``manual_cases.yaml`` — hand-annotated
    #    cases that the importer never touches. Both files are loaded if
    #    present and merged into the same case list.
    for cases_dir in sorted({p.parent for p in cases_root.glob("*/cases.yaml")}
                            | {p.parent for p in cases_root.glob("*/manual_cases.yaml")}):
        dir_name = cases_dir.name
        if dir_name in seen_dirs:
            continue
        if dir_name == "greptile_raw":
            # Internal scraper artefact, never holds runnable cases
            continue
        patch_dir = str(cases_dir)
        # Auto-imported cases (cases.yaml)
        cases_yaml = cases_dir / "cases.yaml"
        if cases_yaml.exists():
            with open(cases_yaml) as f:
                cases_data = yaml.safe_load(f) or {}
            all_cases.extend(_build_case_tuples(cases_data, None, patch_dir, eval_dir))
        # Hand-annotated cases (manual_cases.yaml)
        manual_yaml = cases_dir / "manual_cases.yaml"
        if manual_yaml.exists():
            with open(manual_yaml) as f:
                manual_data = yaml.safe_load(f) or {}
            all_cases.extend(_build_case_tuples(manual_data, None, patch_dir, eval_dir))

    if filter_str:
        all_cases = [(c, s, p) for c, s, p in all_cases if filter_str in c.id]

    return all_cases


def _build_case_tuples(cases_data: dict, default_source: Optional[str],
                       patch_dir: str, eval_dir: Path) -> list:
    """Convert raw cases.yaml dicts into ``(CaseConfig, source_dir, patch_dir)`` tuples.

    Resolves per-case ``source_dir`` overrides relative to ``eval_dir``.
    Cases without a ``source_dir`` field fall back to ``default_source``;
    if that is also missing, the case is dropped with a warning.
    """
    out = []
    for case_def in cases_data.get("cases", []):
        case_source = case_def.get("source_dir")
        if case_source:
            resolved = str(eval_dir / case_source)
        else:
            resolved = default_source
        if not resolved:
            print(
                f"WARNING: case {case_def.get('id', '?')!r} has no source_dir and "
                f"its directory has no repos.yaml entry — skipping",
                file=sys.stderr,
            )
            continue
        case = CaseConfig(
            id=case_def["id"],
            patch=case_def["patch"],
            difficulty=case_def["difficulty"],
            title=case_def["title"],
            description=case_def["description"],
            expected_findings=case_def.get("expected_findings", []),
            source_dir=case_source,
            base_ref=case_def.get("base_ref"),
            head_ref=case_def.get("head_ref"),
        )
        out.append((case, resolved, patch_dir))
    return out


# ---------------------------------------------------------------------------
# Pipeline mode runner
# ---------------------------------------------------------------------------

async def run_single_case(
    case: CaseConfig,
    source_dir: str,
    patch_dir: str,
    provider,
    explorer_provider,
    max_agents: int,
    use_judge: bool,
    judge_provider=None,
    use_brain: bool = True,
    verbose: bool = False,
) -> tuple:
    """Run a single case through the Brain pipeline and return (CaseScore, judge_verdict)."""
    print(f"  [brain] Running {case.id} ({case.difficulty})... ", end="", flush=True)

    run_result = await run_case_brain(
        case=case,
        source_dir=source_dir,
        patch_dir=patch_dir,
        provider=provider,
        explorer_provider=explorer_provider,
    )

    if run_result.error:
        print(f"ERROR: {run_result.error}")
        return CaseScore(case_id=case.id, error=run_result.error), None

    review = run_result.review_result
    findings = review.findings if review else []
    files_reviewed = review.files_reviewed if review else []

    score = score_case(case, findings, files_reviewed)
    print(f"composite={score.composite:.3f} (recall={score.recall:.2f}, findings={len(findings)})")

    if verbose and findings:
        for i, f in enumerate(findings):
            matched = any(m.actual_index == i for m in score.matches)
            marker = "MATCH" if matched else "extra"
            print(
                f"    [{marker:5s}] {f.severity.value:8s} conf={f.confidence:.2f} "
                f"| {f.file}:{f.start_line}-{f.end_line} | agent={f.agent}"
            )
            print(f"            {f.title}")
        if score.matches:
            for m in score.matches:
                exp = case.expected_findings[m.expected_index]
                sev_label = (
                    "exact" if m.severity_match >= 0.99
                    else "adj" if m.severity_match >= 0.49
                    else "miss"
                )
                print(
                    f"    -> expected[{m.expected_index}] matched actual[{m.actual_index}]: "
                    f"title={m.title_match} file={m.file_match} line={m.line_match} "
                    f"sev={sev_label} rec={m.recommendation_match}"
                )

    # LLM judge — pass `case_score` so the judge can see the deterministic
    # match table and apply qualitative scoring on top of it.
    judge_verdict = None
    if use_judge and judge_provider and review:
        print(f"    Judging {case.id}... ", end="", flush=True)
        verdict = judge_case(
            provider=judge_provider,
            case_title=case.title,
            case_description=case.description,
            expected_findings=case.expected_findings,
            findings=findings,
            synthesis=review.synthesis,
            case_score=score,
        )
        judge_verdict = verdict.to_dict()
        judge_verdict["case_id"] = case.id
        print(f"avg={verdict.average:.1f}")

    return score, judge_verdict


# ---------------------------------------------------------------------------
# Gold-standard mode runner
# ---------------------------------------------------------------------------

def _save_gold_trace(case_id: str, trace, timestamp: str) -> str:
    """Save the full gold trace to a JSON file for later analysis.

    Returns path to the saved file.
    """
    _GOLD_TRACES_DIR.mkdir(parents=True, exist_ok=True)
    filepath = _GOLD_TRACES_DIR / f"{case_id}_{timestamp}.json"
    data = trace.to_dict() if trace else {}
    data["case_id"] = case_id
    filepath.write_text(json.dumps(data, indent=2, default=str) + "\n")
    return str(filepath)


async def run_single_gold_case(
    case: CaseConfig,
    source_dir: str,
    patch_dir: str,
    model: str,
    max_budget_usd: float,
    use_judge: bool,
    judge_provider=None,
) -> tuple:
    """Run a single case with Claude Code CLI and return (CaseScore, judge_verdict, trace)."""
    print(f"  [gold] Running {case.id} ({case.difficulty})... ", end="", flush=True)

    gold_result = await run_gold_case(
        case=case,
        source_dir=source_dir,
        patch_dir=patch_dir,
        model=model,
        max_budget_usd=max_budget_usd,
    )

    if gold_result.error:
        print(f"ERROR: {gold_result.error}")
        return CaseScore(case_id=case.id, error=gold_result.error), None, None

    review = gold_result.review_result
    findings = review.findings if review else []
    files_reviewed = review.files_reviewed if review else []
    trace = gold_result.trace

    score = score_case(case, findings, files_reviewed)

    # Print summary
    duration_str = f"{gold_result.duration_seconds:.0f}s" if gold_result.duration_seconds else ""
    tool_count = trace.total_tool_calls if trace else 0
    cost_str = f"${trace.cost_usd:.3f}" if trace and trace.cost_usd else ""
    print(
        f"composite={score.composite:.3f} "
        f"(recall={score.recall:.2f}, findings={len(findings)}, "
        f"tools={tool_count}, {duration_str}, {cost_str})"
    )

    # Print tool usage summary
    if trace and trace.tool_uses:
        tool_counts = {}
        for tu in trace.tool_uses:
            tool_counts[tu.tool_name] = tool_counts.get(tu.tool_name, 0) + 1
        top_tools = sorted(tool_counts.items(), key=lambda x: -x[1])[:5]
        tools_str = ", ".join(f"{name}:{count}" for name, count in top_tools)
        print(f"    Tools: {tools_str}")

    if trace and trace.files_read:
        print(f"    Files read: {len(trace.files_read)}")

    # LLM judge — pass `case_score` so the judge can see the deterministic
    # match table and apply qualitative scoring on top of it.
    judge_verdict = None
    if use_judge and judge_provider and review:
        print(f"    Judging {case.id}... ", end="", flush=True)
        verdict = judge_case(
            provider=judge_provider,
            case_title=case.title,
            case_description=case.description,
            expected_findings=case.expected_findings,
            findings=findings,
            synthesis=review.synthesis,
            case_score=score,
        )
        judge_verdict = verdict.to_dict()
        judge_verdict["case_id"] = case.id
        print(f"avg={verdict.average:.1f}")

    return score, judge_verdict, trace


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def run_all(args: argparse.Namespace) -> None:
    """Run all eval cases and print the final report.

    Dispatches to the appropriate mode (pipeline, brain, or gold-standard)
    based on CLI args, then builds and prints the eval report.  Saves a
    baseline file if ``--save-baseline`` is set and exits with a non-zero
    status code if regressions are detected.

    Args:
        args: Parsed CLI arguments from ``parse_args()``.
    """
    eval_dir = Path(__file__).resolve().parent
    cases = load_cases(eval_dir, args.filter)

    if not cases:
        print("No cases found. Check repos.yaml and cases/ directory.")
        return

    is_gold = args.gold
    if is_gold:
        mode_label = "gold-standard (Claude Code CLI)"
    else:
        mode_label = "brain (PRBrainOrchestrator)"
    print(f"Loaded {len(cases)} case(s) — mode: {mode_label}")

    if is_gold:
        print(f"Gold model: {args.gold_model}, max budget: ${args.gold_max_budget}/case")
    else:
        print(f"Provider: {args.provider}, Model: {args.model or 'default'}")
    print()

    scores = []
    verdicts = []
    traces = []  # gold mode only

    if is_gold:
        # Gold mode: invoke Claude Code CLI (no AIProvider needed)
        # Judge still needs a provider if enabled
        judge_provider = None
        if not args.no_judge:
            judge_provider = create_provider(args.provider, args.model)

        from datetime import datetime, timezone
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

        for case, source_dir, patch_dir in cases:
            score, verdict, trace = await run_single_gold_case(
                case, source_dir, patch_dir,
                args.gold_model,
                args.gold_max_budget,
                not args.no_judge, judge_provider,
            )
            scores.append(score)
            if verdict:
                verdicts.append(verdict)
            if trace:
                trace_path = _save_gold_trace(case.id, trace, ts)
                traces.append(trace_path)

        if traces:
            print(f"\nGold traces saved: {len(traces)} files in {_GOLD_TRACES_DIR}/")
    else:
        # Pipeline mode
        provider = create_provider(args.provider, args.model)
        explorer_provider = None
        if args.explorer_model:
            explorer_provider = create_provider(args.provider, args.explorer_model)
        judge_provider = provider if not args.no_judge else None

        use_brain = getattr(args, 'brain', False)
        verbose = getattr(args, 'verbose', False)

        if args.parallelism <= 1:
            for case, source_dir, patch_dir in cases:
                score, verdict = await run_single_case(
                    case, source_dir, patch_dir,
                    provider, explorer_provider, args.max_agents,
                    not args.no_judge, judge_provider,
                    use_brain=use_brain, verbose=verbose,
                )
                scores.append(score)
                if verdict:
                    verdicts.append(verdict)
        else:
            for i in range(0, len(cases), args.parallelism):
                batch = cases[i:i + args.parallelism]
                tasks = [
                    run_single_case(
                        case, source_dir, patch_dir,
                        provider, explorer_provider, args.max_agents,
                        not args.no_judge, judge_provider,
                        use_brain=use_brain, verbose=verbose,
                    )
                    for case, source_dir, patch_dir in batch
                ]
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for result in results:
                    if isinstance(result, Exception):
                        print(f"  Case failed with exception: {result}")
                        scores.append(CaseScore(case_id="unknown", error=str(result)))
                    else:
                        score, verdict = result
                        scores.append(score)
                        if verdict:
                            verdicts.append(verdict)

    # Load gold baseline for comparison (pipeline mode only)
    gold_baseline = None
    if args.compare_gold and not is_gold:
        gold_baseline = load_latest_gold_baseline()
        if not gold_baseline:
            print("WARNING: --compare-gold set but no gold baseline found in gold_baselines/",
                  file=sys.stderr)

    # Build and print report
    report = build_report(
        scores=scores,
        judge_verdicts=verdicts or None,
        provider="claude-code" if is_gold else args.provider,
        model=args.gold_model if is_gold else (args.model or "default"),
        mode="gold" if is_gold else "brain",
        gold_baseline=gold_baseline,
    )
    print_report(report)

    # Save baseline
    if args.save_baseline:
        if is_gold:
            path = save_gold_baseline(report)
            print(f"Gold baseline saved: {path}")
        else:
            path = save_baseline(report)
            print(f"Baseline saved: {path}")

    # Exit with error code if regressions detected
    if report.has_regressions:
        sys.exit(1)


def main():
    args = parse_args()
    asyncio.run(run_all(args))


if __name__ == "__main__":
    main()
