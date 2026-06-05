"""Fast Bedrock reachability check — direct Converse, hard timeouts, never hangs.

Authenticates exactly like the app: reuses ``sdk_worker.bedrock_env`` (priority
bearer > static > profile > IAM role), applies that env so boto3's default
credential chain picks the same mode, then does one tiny Converse round-trip
against the strong-tier (sonnet) model with strict botocore timeouts and zero
retries. Expired/temporary creds surface in ~1s instead of hanging the CLI path.

Run before eval / SDK tests:  ``make bedrock-check``

Exit codes: 0 = reachable, 2 = no creds resolved, 3 = call failed.
Override the model with ``BEDROCK_CHECK_MODEL=<id>``.
"""

import os
import sys
import time

# Run as a plain script (`python scripts/bedrock_check.py`) → ensure the backend
# package root is importable regardless of cwd.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.agent_loop.sdk_worker import bedrock_env
from app.config import load_config

try:
    from app.agent_loop.sdk_worker import _model_id_for_tier
except Exception:
    _model_id_for_tier = None  # type: ignore[assignment]

_DEFAULT_MODEL = "eu.anthropic.claude-sonnet-4-6"


def _auth_mode(env: dict) -> str:
    # bedrock_env sets cleared keys to None (to UNSET them for the subprocess),
    # so test truthiness, not membership.
    if env.get("AWS_BEARER_TOKEN_BEDROCK"):
        return "bearer token (long-lived Bedrock API key)"
    if env.get("AWS_ACCESS_KEY_ID"):
        return "static keys" + (" + session token (TEMPORARY — expires)" if env.get("AWS_SESSION_TOKEN") else "")
    if env.get("AWS_PROFILE"):
        return f"SSO profile ({env['AWS_PROFILE']}, auto-refresh)"
    return "IAM role / default chain (deployed mode)"


def main() -> int:
    t0 = time.time()
    cfg = load_config()
    env = bedrock_env()

    # Apply the resolved auth env so boto3's default chain authenticates the same
    # way the app does (None REMOVES a key — mirrors the SDK subprocess contract).
    for key, val in env.items():
        if val is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = val

    region = env.get("AWS_REGION") or "eu-west-2"
    # Truthiness, not membership — bedrock_env sets cleared keys to None to UNSET
    # them, so `"AWS_ACCESS_KEY_ID" in env` is True even in profile/role mode.
    has_creds = any(env.get(k) for k in ("AWS_BEARER_TOKEN_BEDROCK", "AWS_ACCESS_KEY_ID", "AWS_PROFILE"))

    model = os.environ.get("BEDROCK_CHECK_MODEL")
    if not model and _model_id_for_tier is not None:
        try:
            model = _model_id_for_tier("strong", cfg)
        except Exception:
            model = None
    model = model or _DEFAULT_MODEL

    print(f"region : {region}")
    print(f"auth   : {_auth_mode(env)}")
    print(f"model  : {model}")

    if not has_creds:
        print(
            "\n❌ FAIL — no local credentials resolved (deployed/IAM-role mode "
            "has none here). Set a bearer token, static keys, or a profile."
        )
        return 2

    import boto3
    from botocore.config import Config as BotoConfig

    client = boto3.client(
        "bedrock-runtime",
        region_name=region,
        config=BotoConfig(connect_timeout=5, read_timeout=25, retries={"max_attempts": 0}),
    )

    try:
        resp = client.converse(
            modelId=model,
            messages=[{"role": "user", "content": [{"text": "Reply with one word: PONG"}]}],
            inferenceConfig={"maxTokens": 16, "temperature": 0},
        )
    except Exception as exc:
        elapsed = time.time() - t0
        print(f"\n❌ FAIL ({elapsed:.1f}s) — {type(exc).__name__}: {exc}")
        msg = str(exc)
        if "ExpiredToken" in msg or type(exc).__name__ == "ExpiredTokenException":
            print(
                "   → temporary creds expired. Refresh with `aws sso login` and re-paste, "
                "or switch to a long-lived bearer token (CONDUCTOR_AWS_BEARER_TOKEN)."
            )
        elif "AccessDenied" in msg or "UnrecognizedClient" in msg:
            print("   → creds rejected. Check the IAM principal has bedrock:InvokeModel " "on this model + region.")
        elif "ThrottlingException" in msg:
            print("   → throttled. Bedrock is reachable; retry shortly.")
        return 3

    out = resp.get("output", {}).get("message", {}).get("content", [])
    reply = " ".join(b.get("text", "").strip() for b in out if "text" in b)
    print(f"\n✅ PASS ({time.time() - t0:.1f}s) — sonnet replied: {reply!r}")
    print(f"   usage: {resp.get('usage')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
