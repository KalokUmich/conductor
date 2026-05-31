#!/usr/bin/env bash
# Set up / refresh AWS SSO so the app uses a PROFILE for Bedrock — boto3 then
# AUTO-REFRESHES the 1-hour role creds from the cached SSO login (~8h), instead of
# pasting a fresh ASIA token every hour.
#
# Usage:  scripts/sso_creds.sh <account_id> [role_name] [sso_session]
#   <account_id>  account that hosts Bedrock (e.g. 533267248474)
#   [role_name]   SSO role to assume (required only if the profile doesn't exist yet)
#   [sso_session] SSO session in ~/.aws/config (default: fintern; e.g. render)
#
# Effect (does NOT touch conductor.secrets.local.yaml):
#   1. ensures  [profile bedrock-<account_id>]  in ~/.aws/config
#   2. `aws sso login` if the cached token is expired (browser, once per ~8h)
#   3. verifies caller-identity resolves <account_id>
#   4. writes  config/conductor.local.env  (gitignored) with
#        CONDUCTOR_AWS_PROFILE=bedrock-<account_id>
#      which check_creds / guarded_run / eval runners source. Idempotent.
set -uo pipefail

ACCT="${1:?usage: sso_creds.sh <account_id> [role_name] [sso_session]}"
ROLE="${2:-}"
SESSION="${3:-fintern}"
REGION="${AWS_BEDROCK_REGION:-eu-west-2}"
PROFILE="bedrock-${ACCT}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="$REPO_ROOT/config/conductor.local.env"

# 1. Ensure the profile exists.
if ! aws configure list-profiles 2>/dev/null | grep -qx "$PROFILE"; then
  if [ -z "$ROLE" ]; then
    echo "Profile '$PROFILE' does not exist and no role_name given."
    echo "Re-run:  scripts/sso_creds.sh $ACCT <role_name> [$SESSION]"
    exit 2
  fi
  echo "Creating [profile $PROFILE]  (sso_session=$SESSION account=$ACCT role=$ROLE region=$REGION)"
  aws configure set "profile.${PROFILE}.sso_session"   "$SESSION"
  aws configure set "profile.${PROFILE}.sso_account_id" "$ACCT"
  aws configure set "profile.${PROFILE}.sso_role_name"  "$ROLE"
  aws configure set "profile.${PROFILE}.region"         "$REGION"
fi

# 2. Ensure a valid SSO login.
if ! aws sts get-caller-identity --profile "$PROFILE" >/dev/null 2>&1; then
  echo "SSO token expired/absent — launching 'aws sso login --sso-session $SESSION' (browser)…"
  aws sso login --sso-session "$SESSION"
fi

# 3. Verify it resolves the right account.
if ! IDENT="$(aws sts get-caller-identity --profile "$PROFILE" 2>&1)"; then
  echo "STILL cannot authenticate with profile '$PROFILE':"; echo "$IDENT"; exit 1
fi
echo "OK: $IDENT"
case "$IDENT" in
  *"$ACCT"*) : ;;
  *) echo "WARNING: caller-identity does not contain account $ACCT — check the role/session." ;;
esac

# 4. Point tooling at the profile (non-secret env file; gitignored).
{
  echo "# Written by scripts/sso_creds.sh — non-secret AWS profile selector."
  echo "export CONDUCTOR_AWS_PROFILE=$PROFILE"
  echo "export CONDUCTOR_AWS_REGION=$REGION"
} > "$ENV_FILE"
echo "Wrote $ENV_FILE (CONDUCTOR_AWS_PROFILE=$PROFILE). Tooling sources it automatically."
echo "Done — app + evals now auto-refresh role creds via SSO profile '$PROFILE' (one login lasts ~8h)."
