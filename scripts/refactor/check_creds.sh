#!/usr/bin/env bash
# Credential gate. Run BEFORE every step that may call Bedrock (eval / PR-brain / tool test).
# Exit 0 = creds valid, proceed. Exit 1 = STOP and refresh.
#
# Two paths:
#   * SSO PROFILE (preferred): if CONDUCTOR_AWS_PROFILE is set (via config/conductor.local.env,
#     written by scripts/sso_creds.sh), validate via boto3 Session(profile) — boto3 auto-refreshes
#     the role creds from the cached SSO login (~8h). No hourly pasting.
#   * STATIC keys (fallback): validate access_key_id/secret in secrets(.local).yaml.
set -uo pipefail
cd "$(dirname "$0")/../.." || exit 1

# Pick up the non-secret profile selector if present.
[ -f config/conductor.local.env ] && . config/conductor.local.env

.venv/bin/python - <<'PY'
import os, sys
sys.path.insert(0, 'backend')
try:
    import boto3
except Exception as e:
    print(f"[creds] cannot import boto3: {e}"); sys.exit(1)

profile = (os.environ.get("CONDUCTOR_AWS_PROFILE") or "").strip()
region = (os.environ.get("CONDUCTOR_AWS_REGION") or "eu-west-2").strip() or "eu-west-2"

# --- Preferred: SSO profile (auto-refresh) ---
if profile:
    try:
        sess = boto3.Session(profile_name=profile)
        ident = sess.client("sts", region_name=region).get_caller_identity()
        print(f"[creds] OK — SSO profile '{profile}' valid (account {ident['Account']}, region {region}); auto-refresh ON.")
        sys.exit(0)
    except Exception as e:
        print(f"[creds] STOP: SSO profile '{profile}' invalid ({type(e).__name__}: {str(e)[:120]})")
        print("[creds] -> run: scripts/sso_creds.sh <account_id> [role] [sso_session]  (it runs `aws sso login` if needed)")
        sys.exit(1)

# --- Fallback: static keys from secrets(.local).yaml ---
try:
    from app.config import _apply_env_overrides, _load_yaml_with_local
except Exception as e:
    print(f"[creds] cannot import config: {e}"); sys.exit(1)

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

ab = find(data, "aws_bedrock") or {}
ak = (ab.get("access_key_id") or "").strip()
sk = (ab.get("secret_access_key") or "").strip()
tok = (ab.get("session_token") or "").strip()
region = ab.get("region") or "eu-west-2"

if not (ak and sk):
    print("[creds] STOP: no CONDUCTOR_AWS_PROFILE and aws_bedrock keys are EMPTY.")
    print("[creds] -> preferred: run `scripts/sso_creds.sh <account_id> [role] [sso_session]` (SSO auto-refresh),")
    print("[creds]    or paste fresh static keys into config/conductor.secrets.local.yaml.")
    sys.exit(1)

try:
    sts = boto3.client("sts", aws_access_key_id=ak, aws_secret_access_key=sk,
                       aws_session_token=tok or None, region_name=region)
    ident = sts.get_caller_identity()
    print(f"[creds] OK — static Bedrock creds valid (account {ident['Account']}, region {region}).")
    sys.exit(0)
except Exception as e:
    print(f"[creds] STOP: static Bedrock creds INVALID ({type(e).__name__}: {str(e)[:120]})")
    print("[creds] -> SSO temp token likely expired. Preferred: `scripts/sso_creds.sh <account_id>` (auto-refresh).")
    sys.exit(1)
PY
