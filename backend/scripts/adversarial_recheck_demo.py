#!/usr/bin/env python3
"""Adversarial finding recheck — standalone host demo.

Runs the adversarial recheck on a PR's ALREADY-POSTED findings using the in-house
AgentLoopService (Opus) judge — so it works on the host venv without a container
rebuild. Dry-run by default (judges + prints; touches nothing). The production
path is the ``/adversarial-recheck`` endpoint (SDK/Opus engine).

Usage:
  python backend/scripts/adversarial_recheck_demo.py --pr 14471 \
      --project Abound --repo abound-server \
      --workspace /home/kalok/abound-server/abound-server \
      [--apply] [--judge-resolved]

``--workspace`` should be a checkout of the repo (for the judge to grep). If
omitted, a blobless clone of the PR source branch is made in a temp dir.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

_BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_BACKEND))


def _load_secrets() -> dict:
    cfg = _BACKEND.parent / "config"
    base = yaml.safe_load((cfg / "conductor.secrets.yaml").read_text()) or {}
    local = cfg / "conductor.secrets.local.yaml"
    if local.is_file():
        over = yaml.safe_load(local.read_text()) or {}

        def merge(b, o):
            for k, v in o.items():
                if isinstance(v, dict) and isinstance(b.get(k), dict):
                    merge(b[k], v)
                else:
                    b[k] = v
            return b

        merge(base, over)
    return base


def _opus_provider(secrets: dict):
    from app.ai_provider.claude_bedrock import ClaudeBedrockProvider
    from app.integrations.azure_devops.adversarial_recheck import OPUS_MODEL_ID

    b = secrets["ai_providers"]["aws_bedrock"]
    region = b.get("region", "eu-west-2")
    profile = os.environ.get("CONDUCTOR_AWS_PROFILE") or b.get("profile") or ""
    if b.get("bearer_token"):
        os.environ["AWS_BEARER_TOKEN_BEDROCK"] = b["bearer_token"]
        return ClaudeBedrockProvider(region_name=region, model_id=OPUS_MODEL_ID)
    if profile:
        return ClaudeBedrockProvider(region_name=region, model_id=OPUS_MODEL_ID, aws_profile=profile)
    return ClaudeBedrockProvider(
        aws_access_key_id=b["access_key_id"],
        aws_secret_access_key=b["secret_access_key"],
        aws_session_token=b.get("session_token"),
        region_name=region,
        model_id=OPUS_MODEL_ID,
    )


def _clone_pr(org_url: str, pat: str, project: str, repo: str, branch: str) -> str:
    url = org_url.replace("https://", f"https://pat:{pat}@") + f"/{project}/_git/{repo}"
    dest = tempfile.mkdtemp(prefix="adv-recheck-")
    subprocess.run(
        ["git", "clone", "--filter=blob:none", "--branch", branch, "--single-branch", url, dest],
        check=True,
        capture_output=True,
        text=True,
    )
    return dest


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pr", type=int, required=True)
    ap.add_argument("--project", default="Abound")
    ap.add_argument("--repo", default="abound-server")
    ap.add_argument("--workspace", default="")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--judge-resolved", action="store_true")
    ap.add_argument("--concurrency", type=int, default=2)
    args = ap.parse_args()

    os.chdir(str(_BACKEND))
    secrets = _load_secrets()
    ado = secrets.get("azure_devops", {})
    org_url, pat = ado.get("org_url", ""), ado.get("pat", "")
    if not org_url or not pat:
        print("ERROR: azure_devops.org_url / pat missing in secrets.", file=sys.stderr)
        return 2

    from app.integrations.azure_devops.adversarial_recheck import (
        extract_findings,
        format_report,
        make_inhouse_judge,
        run_adversarial_recheck,
    )
    from app.integrations.azure_devops.mcp_client import AzureDevOpsClient
    from app.integrations.azure_devops.recheck import parse_review_threads

    client = AzureDevOpsClient(org_url=org_url, pat=pat)
    pr = await client.get_pull_request(args.project, args.repo, args.pr)
    source_branch = pr.get("sourceRefName", "").replace("refs/heads/", "")
    target_branch = pr.get("targetRefName", "").replace("refs/heads/", "")
    diff_spec = f"origin/{target_branch}...origin/{source_branch}"
    print(f"PR #{args.pr}: {source_branch} -> {target_branch}")

    threads = await client.list_threads(args.project, args.repo, args.pr)
    priors = parse_review_threads(threads)
    findings = extract_findings(priors, include_resolved=args.judge_resolved)
    print(f"Parsed {len(priors)} prior comment(s); {len(findings)} vote-driving finding(s) to judge.")
    for f in findings:
        print(f"  - [{f.severity}] {f.title}  @ {f.location}  (thread {f.thread_id}, status {f.status})")
    if not findings:
        print("Nothing to recheck.")
        return 0

    workspace = args.workspace
    cloned = False
    if not workspace:
        print(f"Cloning {source_branch} (blobless)...")
        workspace = _clone_pr(org_url, pat, args.project, args.repo, source_branch)
        cloned = True
    print(f"Judge workspace: {workspace}")

    provider = _opus_provider(secrets)
    print(f"Opus provider health: {provider.health_check()}")

    judge = make_inhouse_judge(provider=provider, worktree=workspace, diff_spec=diff_spec)
    report = await run_adversarial_recheck(
        judge=judge,
        findings=findings,
        task_id=f"demo-{args.project}-pr-{args.pr}",
        client=client,
        project=args.project,
        repo=args.repo,
        pr_id=args.pr,
        apply=args.apply,
        concurrency=args.concurrency,
    )
    print("\n" + format_report(report))
    if cloned:
        subprocess.run(["rm", "-rf", workspace], check=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
