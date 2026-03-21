#!/usr/bin/env python3
"""Compare agent quality: Python tools vs TypeScript tools.

Runs the Conductor agentic loop on the same baseline questions twice:
  1. Python tools  — LocalToolExecutor (backend-direct mode)
  2. TS tools      — TSToolExecutor (complex + AST via Node.js, rest via Python)

Then compares:
  - Final answer quality (same scoring as run_bedrock.py)
  - Tool call sequences (did the LLM choose different paths?)
  - Tool result differences (for overlapping tool+params, did outputs differ?)

Two modes:
  --live      Run both agents independently, compare final answers (default)
  --replay    Run Python agent, then replay its complex/AST calls through TS,
              directly compare outputs without LLM variability

Usage:
    cd /home/kalok/conductor/backend

    # Live comparison — all baselines
    python ../eval/agent_quality/run_tool_parity.py

    # Replay mode — isolate tool output differences
    python ../eval/agent_quality/run_tool_parity.py --replay

    # Specific baseline
    python ../eval/agent_quality/run_tool_parity.py --case render_credit_decision

    # Workflow mode (multi-agent)
    python ../eval/agent_quality/run_tool_parity.py --workflow
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import time
from dataclasses import asdict
from difflib import unified_diff
from pathlib import Path
from typing import Any, Dict, List, Optional

# Ensure backend is on the path
backend_dir = Path(__file__).resolve().parent.parent.parent / "backend"
sys.path.insert(0, str(backend_dir))

EVAL_DIR = Path(__file__).resolve().parent
BASELINE_DIR = EVAL_DIR / "baselines"
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
EXTENSION_DIR = REPO_ROOT / "extension"
TS_RUNNER = EXTENSION_DIR / "tests" / "run_ts_tool.js"

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

logger = logging.getLogger("tool_parity_eval")

# ---------------------------------------------------------------------------
# TS tool sets
# ---------------------------------------------------------------------------

COMPLEX_TOOLS = {
    "get_dependencies", "get_dependents", "test_outline",
    "compressed_view", "trace_variable", "detect_patterns", "module_summary",
}

AST_TOOLS = {
    "file_outline", "find_symbol", "find_references",
    "get_callees", "get_callers", "expand_symbol",
}

TS_TOOLS = COMPLEX_TOOLS | AST_TOOLS


# ---------------------------------------------------------------------------
# TSToolExecutor — routes complex + AST tools to Node.js, rest to Python
# ---------------------------------------------------------------------------

def _run_ts_tool(tool_name: str, workspace: str, params: dict) -> dict:
    """Call the TS runner via subprocess and return parsed JSON result."""
    result = subprocess.run(
        ["node", str(TS_RUNNER), tool_name, workspace, json.dumps(params)],
        capture_output=True, text=True, timeout=60,
        cwd=str(EXTENSION_DIR),
    )
    if result.returncode != 0:
        return {"success": False, "data": None, "error": f"TS runner error: {result.stderr[:500]}"}
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"success": False, "data": None, "error": f"Invalid JSON from TS runner: {result.stdout[:300]}"}


class TSToolExecutor:
    """Routes complex + AST tools to TypeScript, everything else to Python.

    Implements the ToolExecutor interface so it can be injected into
    AgentLoopService.
    """

    def __init__(self, workspace_path: str) -> None:
        from app.code_tools.executor import LocalToolExecutor
        self._workspace_path = workspace_path
        self._local = LocalToolExecutor(workspace_path)

    @property
    def workspace_path(self) -> str:
        return self._workspace_path

    async def execute(self, tool_name: str, params: Dict[str, Any]):
        from app.code_tools.schemas import ToolResult
        if tool_name in TS_TOOLS:
            raw = await asyncio.to_thread(
                _run_ts_tool, tool_name, self._workspace_path, params,
            )
            return ToolResult(
                tool_name=tool_name,
                success=raw.get("success", False),
                data=raw.get("data"),
                error=raw.get("error"),
                truncated=raw.get("truncated", False),
            )
        return await self._local.execute(tool_name, params)


# ---------------------------------------------------------------------------
# Provider setup (same as run_bedrock.py)
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
        region_name=bedrock.get("region", "eu-west-2"),
        model_id=model_id,
    )
    if not provider.health_check():
        logger.error("Provider health check failed for %s", model_id)
        sys.exit(1)
    return provider


# ---------------------------------------------------------------------------
# Scoring (same as run_bedrock.py)
# ---------------------------------------------------------------------------

def score_answer(answer: str, required_findings: list[dict]) -> dict:
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
# Run agent (with tracing)
# ---------------------------------------------------------------------------

async def run_agent_traced(provider, workspace: str, question: str, executor) -> dict:
    """Run AgentLoopService with a TracingToolExecutor and return results + trace."""
    from app.agent_loop.service import AgentLoopService
    from app.agent_loop.budget import BudgetConfig
    from app.code_tools.executor import TracingToolExecutor

    tracing = TracingToolExecutor(executor)
    agent = AgentLoopService(
        provider=provider,
        max_iterations=40,
        budget_config=BudgetConfig(max_input_tokens=500_000),
        tool_executor=tracing,
    )
    result = await agent.run(query=question, workspace_path=workspace)
    return {
        "answer": result.answer,
        "tool_calls": result.tool_calls_made,
        "iterations": result.iterations,
        "duration_ms": result.duration_ms,
        "error": result.error,
        "thinking_steps": [asdict(s) if hasattr(s, "__dataclass_fields__") else s
                           for s in (result.thinking_steps or [])],
        "trace": [asdict(c) for c in tracing.calls],
    }


async def run_workflow_traced(provider, workspace: str, question: str, executor,
                              explorer_provider=None) -> dict:
    """Run workflow engine with tracing."""
    from app.workflow.loader import load_workflow
    from app.workflow.engine import WorkflowEngine
    from app.code_tools.executor import TracingToolExecutor

    tracing = TracingToolExecutor(executor)
    workflow = load_workflow("workflows/code_explorer.yaml")
    engine = WorkflowEngine(
        provider=provider,
        explorer_provider=explorer_provider or provider,
        tool_executor=tracing,
    )
    wf_context = {
        "query_text": question,
        "query": question,
        "workspace_path": workspace,
    }

    start = time.time()
    wf_result = await engine.run(workflow, wf_context)
    elapsed_ms = (time.time() - start) * 1000

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
        "thinking_steps": [],
        "trace": [asdict(c) for c in tracing.calls],
    }


# ---------------------------------------------------------------------------
# Replay mode — run Python trace calls through TS, compare outputs
# ---------------------------------------------------------------------------

async def replay_through_ts(py_trace: list[dict], workspace: str) -> list[dict]:
    """Replay tool calls from the Python trace through TS tools.

    Only replays calls to tools that have TS implementations (13 tools).
    Returns a list of comparison records.
    """
    comparisons = []

    for call in py_trace:
        tool = call["tool_name"]
        if tool not in TS_TOOLS:
            continue

        params = call["params"]
        ts_raw = await asyncio.to_thread(
            _run_ts_tool, tool, workspace, params,
        )

        py_data_str = json.dumps(call["data"], sort_keys=True, default=str)
        ts_data_str = json.dumps(ts_raw.get("data"), sort_keys=True, default=str)

        identical = py_data_str == ts_data_str

        comp = {
            "tool": tool,
            "params": params,
            "py_success": call["success"],
            "ts_success": ts_raw.get("success", False),
            "identical": identical,
            "py_data_chars": len(py_data_str),
            "ts_data_chars": len(ts_data_str),
        }

        if not identical:
            # Generate a diff for small outputs
            if len(py_data_str) < 5000 and len(ts_data_str) < 5000:
                diff = list(unified_diff(
                    py_data_str.splitlines(keepends=True),
                    ts_data_str.splitlines(keepends=True),
                    fromfile="python",
                    tofile="typescript",
                    n=2,
                ))
                comp["diff"] = "".join(diff[:50])  # cap diff lines
            else:
                comp["diff"] = "(output too large for inline diff)"

        comparisons.append(comp)

    return comparisons


# ---------------------------------------------------------------------------
# Comparison helpers
# ---------------------------------------------------------------------------

def compare_tool_sequences(py_trace: list[dict], ts_trace: list[dict]) -> dict:
    """Compare which tools each mode called and in what order."""
    py_tools = [c["tool_name"] for c in py_trace]
    ts_tools = [c["tool_name"] for c in ts_trace]

    py_ts_calls = [t for t in py_tools if t in TS_TOOLS]
    ts_ts_calls = [t for t in ts_tools if t in TS_TOOLS]

    # Find overlapping calls (same tool+params in both traces)
    py_keys = [(c["tool_name"], json.dumps(c["params"], sort_keys=True))
               for c in py_trace if c["tool_name"] in TS_TOOLS]
    ts_keys = [(c["tool_name"], json.dumps(c["params"], sort_keys=True))
               for c in ts_trace if c["tool_name"] in TS_TOOLS]

    py_set = set(py_keys)
    ts_set = set(ts_keys)
    overlap = py_set & ts_set

    return {
        "py_total_calls": len(py_tools),
        "ts_total_calls": len(ts_tools),
        "py_ts_tool_calls": len(py_ts_calls),
        "ts_ts_tool_calls": len(ts_ts_calls),
        "py_ts_tools_used": sorted(set(py_ts_calls)),
        "ts_ts_tools_used": sorted(set(ts_ts_calls)),
        "overlapping_calls": len(overlap),
        "py_only_ts_calls": len(py_set - ts_set),
        "ts_only_ts_calls": len(ts_set - py_set),
        "py_sequence": py_tools,
        "ts_sequence": ts_tools,
    }


def compare_overlapping_results(py_trace: list[dict], ts_trace: list[dict]) -> list[dict]:
    """For tool calls that appear in both runs, compare outputs."""
    py_by_key = {}
    for c in py_trace:
        if c["tool_name"] in TS_TOOLS:
            key = (c["tool_name"], json.dumps(c["params"], sort_keys=True))
            if key not in py_by_key:
                py_by_key[key] = c

    ts_by_key = {}
    for c in ts_trace:
        if c["tool_name"] in TS_TOOLS:
            key = (c["tool_name"], json.dumps(c["params"], sort_keys=True))
            if key not in ts_by_key:
                ts_by_key[key] = c

    diffs = []
    for key in py_by_key:
        if key not in ts_by_key:
            continue
        py_c = py_by_key[key]
        ts_c = ts_by_key[key]

        py_data_str = json.dumps(py_c["data"], sort_keys=True, default=str)
        ts_data_str = json.dumps(ts_c["data"], sort_keys=True, default=str)

        identical = py_data_str == ts_data_str
        if not identical:
            diff_lines = list(unified_diff(
                py_data_str.splitlines(keepends=True),
                ts_data_str.splitlines(keepends=True),
                fromfile="python", tofile="typescript", n=2,
            ))
            diffs.append({
                "tool": key[0],
                "params": json.loads(key[1]),
                "py_success": py_c["success"],
                "ts_success": ts_c["success"],
                "diff": "".join(diff_lines[:30]),
            })

    return diffs


# ---------------------------------------------------------------------------
# Baselines
# ---------------------------------------------------------------------------

def load_baselines(case_filter: str | None = None) -> list[dict]:
    baselines = []
    for f in sorted(BASELINE_DIR.glob("*.json")):
        data = json.loads(f.read_text())
        if case_filter and data["id"] != case_filter:
            continue
        baselines.append(data)
    return baselines


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def print_score_report(case_id: str, label: str, run_result: dict, scoring: dict):
    print(f"\n{'='*60}")
    print(f"  {case_id} — {label}")
    print(f"{'='*60}")

    if run_result.get("error"):
        print(f"  ERROR: {run_result['error']}")
        return

    print(f"  Tool calls: {run_result['tool_calls']}")
    print(f"  Duration:   {run_result['duration_ms']:.0f} ms")
    print(f"  Score:      {scoring['total_score']:.1%}")
    print()

    for f in scoring["findings"]:
        icon = "PASS" if f["score"] >= 1.0 else ("PARTIAL" if f["score"] > 0 else "MISS")
        print(f"  [{icon:7s}] {f['id']} ({f['weight']:.0%}) — {f['matched']}/{f['min_required']} patterns")


def print_replay_report(case_id: str, comparisons: list[dict]):
    print(f"\n{'='*60}")
    print(f"  {case_id} — REPLAY (Python → TS)")
    print(f"{'='*60}")

    total = len(comparisons)
    identical = sum(1 for c in comparisons if c["identical"])
    print(f"  TS tool calls replayed: {total}")
    print(f"  Identical outputs:      {identical}/{total}")

    if identical < total:
        print(f"\n  DIFFERENCES:")
        for c in comparisons:
            if not c["identical"]:
                print(f"\n  {c['tool']}({json.dumps(c['params'])[:80]})")
                print(f"    py: success={c['py_success']}  {c['py_data_chars']} chars")
                print(f"    ts: success={c['ts_success']}  {c['ts_data_chars']} chars")
                if c.get("diff"):
                    for line in c["diff"].split("\n")[:10]:
                        print(f"    {line}")


def print_live_comparison(case_id: str, py_result: dict, ts_result: dict,
                          py_scoring: dict, ts_scoring: dict,
                          seq_comp: dict, result_diffs: list[dict]):
    print(f"\n{'='*60}")
    print(f"  {case_id} — LIVE COMPARISON")
    print(f"{'='*60}")

    # Score comparison
    py_s = py_scoring["total_score"]
    ts_s = ts_scoring["total_score"]
    delta = ts_s - py_s
    arrow = "▲" if delta > 0 else ("▼" if delta < 0 else "=")
    print(f"\n  Scores:  Python={py_s:.1%}  TS={ts_s:.1%}  delta={delta:+.1%} {arrow}")

    # Per-finding comparison
    print(f"\n  {'Finding':<30s} {'Python':>8s} {'TS':>8s} {'Delta':>8s}")
    print(f"  {'-'*54}")
    for pf, tf in zip(py_scoring["findings"], ts_scoring["findings"]):
        d = tf["score"] - pf["score"]
        tag = ""
        if d < 0:
            tag = " ◄"
        elif d > 0:
            tag = " ►"
        print(f"  {pf['id']:<30s} {pf['score']:>7.0%} {tf['score']:>8.0%} {d:>+7.0%}{tag}")

    # Tool call stats
    print(f"\n  Tool calls: Python={py_result['tool_calls']}  TS={ts_result['tool_calls']}")
    print(f"  Duration:   Python={py_result['duration_ms']:.0f}ms  TS={ts_result['duration_ms']:.0f}ms")

    # Sequence comparison
    print(f"\n  TS-implemented tool calls: Python made {seq_comp['py_ts_tool_calls']}, TS made {seq_comp['ts_ts_tool_calls']}")
    print(f"  Overlapping calls (same tool+params): {seq_comp['overlapping_calls']}")

    # Result diffs for overlapping calls
    if result_diffs:
        print(f"\n  Tool output differences ({len(result_diffs)} calls with different output):")
        for d in result_diffs[:5]:
            print(f"    {d['tool']}({json.dumps(d['params'])[:60]})")
            if d.get("diff"):
                for line in d["diff"].split("\n")[:5]:
                    print(f"      {line}")


# ---------------------------------------------------------------------------
# Preflight check
# ---------------------------------------------------------------------------

def check_ts_runner():
    """Verify the TS runner is available and compiled."""
    if not TS_RUNNER.exists():
        logger.error("TS runner not found at %s", TS_RUNNER)
        sys.exit(1)

    compiled = EXTENSION_DIR / "out" / "services" / "complexToolRunner.js"
    if not compiled.exists():
        logger.error(
            "Extension not compiled. Run: cd %s && npm run compile",
            EXTENSION_DIR,
        )
        sys.exit(1)

    # Quick smoke test
    result = subprocess.run(
        ["node", str(TS_RUNNER), "list"],
        capture_output=True, text=True, timeout=10,
        cwd=str(EXTENSION_DIR),
    )
    if result.returncode != 0:
        logger.error("TS runner smoke test failed: %s", result.stderr)
        sys.exit(1)

    tools = json.loads(result.stdout)
    logger.info(
        "TS runner OK: %d complex + %d ast tools",
        len(tools.get("complex", [])), len(tools.get("ast", [])),
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main():
    parser = argparse.ArgumentParser(description="Tool parity agent quality evaluation")
    parser.add_argument("--case", help="Run specific baseline case ID")
    parser.add_argument("--replay", action="store_true",
                        help="Replay Python trace through TS tools (no second LLM run)")
    parser.add_argument("--workflow", action="store_true", help="Use workflow engine")
    parser.add_argument("--haiku", action="store_true",
                        help="Use Haiku as explorer, Sonnet as judge (workflow only)")
    args = parser.parse_args()

    check_ts_runner()

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

        logger.info("Case: %s", case_id)
        logger.info("Question: %s", question)

        from app.code_tools.executor import LocalToolExecutor

        if args.replay:
            # ---- REPLAY MODE ----
            # 1. Run Python agent with tracing
            logger.info("Running Python agent...")
            py_executor = LocalToolExecutor(workspace_path=workspace)

            if args.workflow:
                py_result = await run_workflow_traced(
                    provider, workspace, question, py_executor,
                    explorer_provider=explorer_provider)
            else:
                py_result = await run_agent_traced(
                    provider, workspace, question, py_executor)

            py_scoring = score_answer(py_result["answer"], required)
            print_score_report(case_id, "python", py_result, py_scoring)

            # 2. Replay Python's TS-tool calls through TS runner
            logger.info("Replaying %d calls through TS...", len(py_result["trace"]))
            comparisons = await replay_through_ts(py_result["trace"], workspace)
            print_replay_report(case_id, comparisons)

            all_results[case_id] = {
                "mode": "replay",
                "python": {"run": _strip_trace(py_result), "scoring": py_scoring},
                "replay": comparisons,
            }

        else:
            # ---- LIVE MODE ----
            # 1. Run Python agent
            logger.info("Running Python agent...")
            py_executor = LocalToolExecutor(workspace_path=workspace)

            if args.workflow:
                py_result = await run_workflow_traced(
                    provider, workspace, question, py_executor,
                    explorer_provider=explorer_provider)
            else:
                py_result = await run_agent_traced(
                    provider, workspace, question, py_executor)

            py_scoring = score_answer(py_result["answer"], required)
            print_score_report(case_id, "python", py_result, py_scoring)

            # 2. Run TS agent
            logger.info("Running TS agent...")
            ts_executor = TSToolExecutor(workspace_path=workspace)

            if args.workflow:
                ts_result = await run_workflow_traced(
                    provider, workspace, question, ts_executor,
                    explorer_provider=explorer_provider)
            else:
                ts_result = await run_agent_traced(
                    provider, workspace, question, ts_executor)

            ts_scoring = score_answer(ts_result["answer"], required)
            print_score_report(case_id, "typescript", ts_result, ts_scoring)

            # 3. Compare
            seq_comp = compare_tool_sequences(py_result["trace"], ts_result["trace"])
            result_diffs = compare_overlapping_results(
                py_result["trace"], ts_result["trace"])

            print_live_comparison(
                case_id, py_result, ts_result,
                py_scoring, ts_scoring, seq_comp, result_diffs)

            all_results[case_id] = {
                "mode": "live",
                "python": {"run": _strip_trace(py_result), "scoring": py_scoring},
                "typescript": {"run": _strip_trace(ts_result), "scoring": ts_scoring},
                "comparison": {
                    "sequence": seq_comp,
                    "result_diffs": result_diffs,
                },
            }

    # Save results
    out_file = EVAL_DIR / "results_tool_parity.json"
    with open(out_file, "w") as f:
        json.dump(all_results, f, indent=2, default=str)
    logger.info("Results saved to %s", out_file)

    # Summary — three-way comparison with baselines
    print(f"\n{'='*70}")
    print("  SUMMARY (Baseline vs Python vs TypeScript)")
    print(f"{'='*70}")
    print(f"  {'Case':<30s} {'Baseline':>8s} {'Python':>8s} {'TS':>8s} {'Δ(ts-py)':>8s}")
    print(f"  {'-'*62}")
    for case_id, data in all_results.items():
        # Look up baseline from loaded data
        bl_score = "n/a"
        for bl in baselines:
            if bl["id"] == case_id:
                bl_score = "100%"  # baselines define the gold standard
                break
        mode = data["mode"]
        py_s = data["python"]["scoring"]["total_score"]
        py_c = data["python"]["run"]["tool_calls"]
        if mode == "live":
            ts_s = data["typescript"]["scoring"]["total_score"]
            ts_c = data["typescript"]["run"]["tool_calls"]
            delta = ts_s - py_s
            arrow = "▲" if delta > 0 else ("▼" if delta < 0 else "=")
            print(f"  {case_id:<30s} {bl_score:>8s} {py_s:>7.1%} {ts_s:>8.1%} {delta:>+7.1%}{arrow}")
        else:
            replay = data.get("replay", [])
            total = len(replay)
            identical = sum(1 for c in replay if c["identical"])
            print(f"  {case_id:<30s} {bl_score:>8s} {py_s:>7.1%}    (replay: {identical}/{total} identical)")
    # Averages
    py_scores = [d["python"]["scoring"]["total_score"] for d in all_results.values()]
    ts_scores = [d["typescript"]["scoring"]["total_score"]
                 for d in all_results.values() if "typescript" in d]
    if py_scores and ts_scores:
        py_avg = sum(py_scores) / len(py_scores)
        ts_avg = sum(ts_scores) / len(ts_scores)
        delta_avg = ts_avg - py_avg
        arrow = "▲" if delta_avg > 0 else ("▼" if delta_avg < 0 else "=")
        print(f"  {'-'*62}")
        print(f"  {'AVERAGE':<30s} {'100%':>8s} {py_avg:>7.1%} {ts_avg:>8.1%} {delta_avg:>+7.1%}{arrow}")


def _strip_trace(result: dict) -> dict:
    """Remove full trace data from result for JSON output (too large)."""
    r = dict(result)
    r.pop("trace", None)
    r.pop("thinking_steps", None)
    return r


if __name__ == "__main__":
    os.chdir(str(backend_dir))
    asyncio.run(main())
