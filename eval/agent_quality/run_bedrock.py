#!/usr/bin/env python3
"""Evaluate agent answer quality against baselines.

Runs the Conductor agentic loop on baseline questions and scores the answers
against required findings defined in the baseline JSON files.

Usage:
    cd /home/kalok/conductor/backend

    # Run all baselines (direct agent only, ~30s per case)
    python ../eval/agent_quality/run_bedrock.py

    # Run a specific baseline
    python ../eval/agent_quality/run_bedrock.py --case abound_render_approval

    # Run with workflow engine (multi-agent)
    python ../eval/agent_quality/run_bedrock.py --workflow

    # Compare direct agent vs workflow
    python ../eval/agent_quality/run_bedrock.py --compare
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import time
from pathlib import Path

# Ensure backend is on the path
# eval/agent_quality/ → eval/ → conductor/ → conductor/backend
backend_dir = Path(__file__).resolve().parent.parent.parent / "backend"
sys.path.insert(0, str(backend_dir))

EVAL_DIR = Path(__file__).resolve().parent
BASELINE_DIR = EVAL_DIR / "baselines"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logging.getLogger("botocore").setLevel(logging.WARNING)
logging.getLogger("boto3").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)

logger = logging.getLogger("agent_eval")


# ---------------------------------------------------------------------------
# Provider setup
# ---------------------------------------------------------------------------

def _create_provider(model_id: str = "eu.anthropic.claude-sonnet-4-6"):
    """Create a Bedrock provider from conductor.secrets.yaml."""
    import yaml
    from app.ai_provider.claude_bedrock import ClaudeBedrockProvider

    secrets_path = backend_dir.parent / "config" / "conductor.secrets.yaml"
    with open(secrets_path) as f:
        secrets = yaml.safe_load(f)

    bedrock = secrets["ai_providers"]["aws_bedrock"]
    provider = ClaudeBedrockProvider(
        aws_access_key_id=bedrock["access_key_id"],
        aws_secret_access_key=bedrock["secret_access_key"],
        aws_session_token=bedrock.get("session_token"),
        region_name="eu-west-2",  # inference profiles require eu-west-2
        model_id=model_id,
    )
    if not provider.health_check():
        logger.error("Provider health check failed for %s", model_id)
        sys.exit(1)
    return provider


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def score_answer(answer: str, required_findings: list[dict]) -> dict:
    """Score an answer against required findings.

    Returns a dict with total_score (0-1), per-finding scores, and details.
    """
    results = []
    total_weighted = 0.0
    total_weight = 0.0

    for finding in required_findings:
        fid = finding["id"]
        weight = finding["weight"]
        patterns = finding["check_patterns"]
        min_matches = finding["min_matches"]
        total_weight += weight

        matched = 0
        matched_patterns = []
        for pat in patterns:
            if re.search(pat, answer, re.IGNORECASE):
                matched += 1
                matched_patterns.append(pat)

        score = min(matched / min_matches, 1.0) if min_matches > 0 else 1.0
        weighted = score * weight
        total_weighted += weighted

        results.append({
            "id": fid,
            "description": finding["description"],
            "weight": weight,
            "score": round(score, 2),
            "matched": matched,
            "min_required": min_matches,
            "matched_patterns": matched_patterns,
        })

    return {
        "total_score": round(total_weighted / total_weight, 3) if total_weight > 0 else 0,
        "findings": results,
    }


# ---------------------------------------------------------------------------
# Run agent
# ---------------------------------------------------------------------------

async def run_direct_agent(provider, workspace: str, question: str) -> dict:
    """Run direct AgentLoopService."""
    from app.agent_loop.service import AgentLoopService
    from app.agent_loop.budget import BudgetConfig
    from app.code_tools.executor import LocalToolExecutor

    executor = LocalToolExecutor(workspace_path=workspace)
    agent = AgentLoopService(
        provider=provider,
        max_iterations=40,
        budget_config=BudgetConfig(max_input_tokens=500_000),
        tool_executor=executor,
    )
    result = await agent.run(query=question, workspace_path=workspace)
    return {
        "answer": result.answer,
        "tool_calls": result.tool_calls_made,
        "iterations": result.iterations,
        "duration_ms": result.duration_ms,
        "error": result.error,
    }


async def run_brain(provider, workspace: str, question: str, explorer_provider=None) -> dict:
    """Run Brain orchestrator mode."""
    from app.workflow.engine import WorkflowEngine
    from app.code_tools.executor import LocalToolExecutor

    executor = LocalToolExecutor(workspace_path=workspace)
    engine = WorkflowEngine(
        provider=provider,
        explorer_provider=explorer_provider or provider,
        tool_executor=executor,
        interactive=False,  # no ask_user in eval
    )

    brain_context = {
        "query_text": question,
        "query": question,
        "workspace_path": workspace,
    }

    start = time.time()
    answer = ""
    total_calls = 0
    async for event in engine.run_brain_stream(brain_context):
        if event.kind == "done" and event.data.get("answer"):
            answer = event.data["answer"]
            total_calls = event.data.get("tool_calls_made", 0)
    elapsed_ms = (time.time() - start) * 1000

    return {
        "answer": answer,
        "tool_calls": total_calls,
        "iterations": 0,
        "duration_ms": elapsed_ms,
        "error": None if answer else "No answer produced",
    }


async def run_workflow(provider, workspace: str, question: str, explorer_provider=None) -> dict:
    """Run full workflow engine."""
    from app.workflow.loader import load_workflow
    from app.workflow.engine import WorkflowEngine
    from app.code_tools.executor import LocalToolExecutor

    executor = LocalToolExecutor(workspace_path=workspace)
    workflow = load_workflow("workflows/code_explorer.yaml")
    engine = WorkflowEngine(
        provider=provider,
        explorer_provider=explorer_provider or provider,
        tool_executor=executor,
    )

    wf_context = {
        "query_text": question,
        "query": question,
        "workspace_path": workspace,
    }

    start = time.time()
    wf_result = await engine.run(workflow, wf_context)
    elapsed_ms = (time.time() - start) * 1000

    # Extract answer
    stage_results = wf_result.get("_stage_results", {})
    answer = ""
    total_calls = 0

    synth = stage_results.get("synthesize", {})
    for _, result in synth.items():
        if isinstance(result, dict) and result.get("answer"):
            answer = result["answer"]
            break

    if not answer:
        for stage_name in ("explore", "investigate"):
            for _, result in stage_results.get(stage_name, {}).items():
                if isinstance(result, dict) and result.get("answer"):
                    answer = result["answer"]
                    total_calls += result.get("tool_calls_made", 0)

    for stage_name in ("explore", "investigate"):
        for _, result in stage_results.get(stage_name, {}).items():
            if isinstance(result, dict):
                total_calls += result.get("tool_calls_made", 0)

    return {
        "answer": answer,
        "tool_calls": total_calls,
        "iterations": 0,
        "duration_ms": elapsed_ms,
        "error": None,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def load_baselines(case_filter: str | None = None) -> list[dict]:
    baselines = []
    for f in sorted(BASELINE_DIR.glob("*.json")):
        data = json.loads(f.read_text())
        if case_filter and data["id"] != case_filter:
            continue
        baselines.append(data)
    return baselines


def print_report(case_id: str, mode: str, run_result: dict, scoring: dict):
    print(f"\n{'='*60}")
    print(f"  {case_id} — {mode}")
    print(f"{'='*60}")

    if run_result.get("error"):
        print(f"  ERROR: {run_result['error']}")
        return

    print(f"  Tool calls: {run_result['tool_calls']}")
    print(f"  Iterations: {run_result['iterations']}")
    print(f"  Duration:   {run_result['duration_ms']:.0f} ms")
    print(f"  Score:      {scoring['total_score']:.1%}")
    print()

    for f in scoring["findings"]:
        icon = "PASS" if f["score"] >= 1.0 else ("PARTIAL" if f["score"] > 0 else "MISS")
        print(f"  [{icon:7s}] {f['id']} ({f['weight']:.0%}) — {f['matched']}/{f['min_required']} patterns")
        if f["score"] < 1.0:
            print(f"           {f['description']}")

    # Show answer preview
    answer = run_result.get("answer", "")
    preview = answer[:300].replace("\n", " ")
    print(f"\n  Answer: {preview}...")


async def main():
    parser = argparse.ArgumentParser(description="Agent quality evaluation")
    parser.add_argument("--case", help="Run specific baseline case ID")
    parser.add_argument("--workflow", action="store_true", help="Use workflow engine")
    parser.add_argument("--brain", action="store_true", help="Use Brain orchestrator")
    parser.add_argument("--compare", action="store_true", help="Run both direct and workflow")
    parser.add_argument("--haiku", action="store_true", help="Use Haiku as explorer, Sonnet as judge")
    args = parser.parse_args()

    baselines = load_baselines(args.case)
    if not baselines:
        logger.error("No baselines found in %s", BASELINE_DIR)
        sys.exit(1)

    provider = _create_provider("eu.anthropic.claude-sonnet-4-6")
    explorer_provider = None
    if args.haiku:
        explorer_provider = _create_provider("eu.anthropic.claude-haiku-4-5-20251001-v1:0")
        logger.info("Workflow mode: explorer=haiku-4-5, judge=sonnet-4-6")
    all_results = {}

    for baseline in baselines:
        case_id = baseline["id"]
        workspace = baseline["workspace"]
        question = baseline["question"]
        required = baseline["required_findings"]

        logger.info("Running case: %s", case_id)
        logger.info("Question: %s", question)

        modes = []
        if args.compare:
            modes = ["direct", "workflow", "brain"]
        elif args.brain:
            modes = ["brain"]
        elif args.workflow:
            modes = ["workflow"]
        else:
            modes = ["direct"]

        case_results = {}
        for mode in modes:
            logger.info("Mode: %s", mode)
            if mode == "direct":
                run_result = await run_direct_agent(provider, workspace, question)
                label = mode
            elif mode == "brain":
                run_result = await run_brain(provider, workspace, question, explorer_provider=explorer_provider)
                label = f"{mode} [haiku→sonnet]" if args.haiku else mode
            else:
                run_result = await run_workflow(provider, workspace, question, explorer_provider=explorer_provider)
                label = f"{mode} [haiku→sonnet]" if args.haiku else mode

            scoring = score_answer(run_result["answer"], required)
            print_report(case_id, label, run_result, scoring)

            case_results[mode] = {
                "run": run_result,
                "scoring": scoring,
            }

        all_results[case_id] = case_results

    # Save results
    out_file = EVAL_DIR / "results_bedrock.json"
    with open(out_file, "w") as f:
        json.dump(all_results, f, indent=2, default=str)
    logger.info("Results saved to %s", out_file)

    # Summary
    print(f"\n{'='*60}")
    print("  SUMMARY")
    print(f"{'='*60}")
    for case_id, modes in all_results.items():
        for mode, data in modes.items():
            s = data["scoring"]["total_score"]
            t = data["run"]["duration_ms"]
            c = data["run"]["tool_calls"]
            print(f"  {case_id}/{mode}: score={s:.1%}  calls={c}  time={t:.0f}ms")


if __name__ == "__main__":
    os.chdir(str(backend_dir))
    asyncio.run(main())
