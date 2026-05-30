#!/usr/bin/env bash
# Credential gate for the agent-SDK refactor.
# Run BEFORE every step that may call Bedrock (any eval / PR-brain / tool test).
# Exit 0 = creds valid, proceed. Exit 1 = STOP and ask the user to refresh.
#
# Per user rule (2026-05-30): before each test, verify the token in
# config/conductor.secrets.local.yaml still works; if not, halt and wait for
# the user to update it, then resume.
set -uo pipefail
cd "$(dirname "$0")/../.." || exit 1

.venv/bin/python - <<'PY'
import sys; sys.path.insert(0, 'backend')
try:
    from app.config import _load_yaml_with_local, _apply_env_overrides
    import boto3
except Exception as e:
    print(f"[creds] cannot import config/boto3: {e}"); sys.exit(1)

data = _load_yaml_with_local("conductor.secrets.yaml"); _apply_env_overrides(data)

def find(d, k):
    if isinstance(d, dict):
        if k in d:
            return d[k]
        for v in d.values():
            r = find(v, k)
            if r is not None:
                return r
    return None

ab = find(data, 'aws_bedrock') or {}
ak = (ab.get('access_key_id') or '').strip()
sk = (ab.get('secret_access_key') or '').strip()
tok = (ab.get('session_token') or '').strip()
region = ab.get('region') or 'eu-west-2'

if not (ak and sk):
    print("[creds] STOP: aws_bedrock access_key_id/secret_access_key are EMPTY in secrets(.local).yaml")
    print("[creds] -> run `aws sso login`, refresh config/conductor.secrets.local.yaml, then resume.")
    sys.exit(1)

try:
    sts = boto3.client('sts', aws_access_key_id=ak, aws_secret_access_key=sk,
                       aws_session_token=tok or None, region_name=region)
    ident = sts.get_caller_identity()
    print(f"[creds] OK — Bedrock creds valid (account {ident['Account']}, region {region}).")
    sys.exit(0)
except Exception as e:
    print(f"[creds] STOP: Bedrock creds INVALID ({type(e).__name__}: {str(e)[:120]})")
    print("[creds] -> SSO temporary token likely expired. Run `aws sso login`,")
    print("[creds]    refresh config/conductor.secrets.local.yaml (ASIA…/session_token), then resume.")
    sys.exit(1)
PY
