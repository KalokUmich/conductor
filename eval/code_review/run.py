#!/usr/bin/env python3
"""CLI entrypoint for the code review eval system.

Two modes:

  Pipeline mode (default):
    Runs CodeReviewService (10-step multi-agent pipeline) against eval cases.
    python eval/run.py --provider anthropic --model claude-sonnet-4-20250514

  Gold-standard mode (--gold):
    Invokes Claude Code CLI directly (own tools, own strategies).
    Produces the quality ceiling baseline for comparison.
    python eval/run.py --gold --save-baseline
    python eval/run.py --gold --gold-model opus --filter "requests-001"

Examples:
    python eval/run.py --filter "requests-001"
    python eval/run.py --no-judge
    python eval/run.py --save-baseline
    python eval/run.py --gold --save-baseline
    python eval/run.py --compare-gold
    python eval/run.py --provider bedrock --model us.anthropic.claude-sonnet-4-5-20250929-v1:0
"""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

import yaml

# Ensure eval/ is on sys.path for local imports
_EVAL_DIR = Path(__file__).resolve().parent
if str(_EVAL_DIR) not in sys.path:
    sys.path.insert(0, str(_EVAL_DIR))

# Backend imports (runner adds backend/ to sys.path)
from runner import CaseConfig, RunResult, run_case, run_case_brain  # noqa: E402
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
        "--brain", action="store_true",
        help="Use PR Brain orchestrator instead of CodeReviewService pipeline",
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
    """Create an AIProvider instance based on CLI args."""
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
        return ClaudeBedrockProvider(
            aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID", ""),
            aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY", ""),
            aws_session_token=os.environ.get("AWS_SESSION_TOKEN"),
            region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
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
    """Load all case configs from repos.yaml + per-repo cases.yaml."""
    repos_path = eval_dir / "repos.yaml"
    with open(repos_path) as f:
        repos_config = yaml.safe_load(f)

    all_cases = []
    for repo_name, repo_info in repos_config.get("repos", {}).items():
        cases_path = eval_dir / "cases" / repo_name / "cases.yaml"
        if not cases_path.exists():
            print(f"WARNING: No cases.yaml for repo '{repo_name}'", file=sys.stderr)
            continue

        with open(cases_path) as f:
            cases_data = yaml.safe_load(f)

        source_dir = str(eval_dir / repo_info["source_dir"])
        patch_dir = str(eval_dir / "cases" / repo_name)

        for case_def in cases_data.get("cases", []):
            case = CaseConfig(
                id=case_def["id"],
                patch=case_def["patch"],
                difficulty=case_def["difficulty"],
                title=case_def["title"],
                description=case_def["description"],
                expected_findings=case_def.get("expected_findings", []),
            )
            all_cases.append((case, source_dir, patch_dir))

    if filter_str:
        all_cases = [(c, s, p) for c, s, p in all_cases if filter_str in c.id]

    return all_cases


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
    use_brain: bool = False,
    verbose: bool = False,
) -> tuple:
    """Run a single case through the pipeline and return (CaseScore, judge_verdict)."""
    mode_label = "[brain]" if use_brain else "[legacy]"
    print(f"  {mode_label} Running {case.id} ({case.difficulty})... ", end="", flush=True)

    if use_brain:
        run_result = await run_case_brain(
            case=case,
            source_dir=source_dir,
            patch_dir=patch_dir,
            provider=provider,
            explorer_provider=explorer_provider,
        )
    else:
        run_result = await run_case(
            case=case,
            source_dir=source_dir,
            patch_dir=patch_dir,
            provider=provider,
            explorer_provider=explorer_provider,
            max_agents=max_agents,
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
                print(
                    f"    -> expected[{m.expected_index}] matched actual[{m.actual_index}]: "
                    f"title={m.title_match} file={m.file_match} line={m.line_match} "
                    f"sev={m.severity_match} rec={m.recommendation_match}"
                )

    # LLM judge
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

    # LLM judge
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
        )
        judge_verdict = verdict.to_dict()
        judge_verdict["case_id"] = case.id
        print(f"avg={verdict.average:.1f}")

    return score, judge_verdict, trace


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def run_all(args: argparse.Namespace) -> None:
    """Main async entry point."""
    eval_dir = Path(__file__).resolve().parent
    cases = load_cases(eval_dir, args.filter)

    if not cases:
        print("No cases found. Check repos.yaml and cases/ directory.")
        return

    is_gold = args.gold
    is_brain = getattr(args, 'brain', False)
    if is_gold:
        mode_label = "gold-standard (Claude Code CLI)"
    elif is_brain:
        mode_label = "brain (PRBrainOrchestrator)"
    else:
        mode_label = "pipeline (CodeReviewService)"
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
        mode="gold" if is_gold else ("brain" if is_brain else "pipeline"),
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
