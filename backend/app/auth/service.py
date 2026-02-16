"""AWS SSO service using OIDC device authorization flow.

Implements the same flow as ``aws sso login``:
1. Register an OIDC client
2. Start device authorization (user gets a verification URL)
3. Poll for token completion
4. Use the access token to discover identity via SSO + STS APIs
"""
import logging
import re

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)


class SSOService:
    """Handles AWS SSO OIDC device authorization and identity discovery."""

    def __init__(self, start_url: str, region: str = "us-east-1"):
        self.start_url = start_url
        self.region = region
        self._oidc_client = boto3.client("sso-oidc", region_name=region)

    def register_and_start(self) -> dict:
        """Register an OIDC client and start device authorization.

        Returns:
            Dict with verification_uri_complete, user_code, device_code,
            client_id, client_secret, expires_in, and interval.
        """
        # Step 1: Register a public OIDC client
        reg = self._oidc_client.register_client(
            clientName="conductor-vscode",
            clientType="public",
        )
        client_id = reg["clientId"]
        client_secret = reg["clientSecret"]

        # Step 2: Start device authorization
        auth = self._oidc_client.start_device_authorization(
            clientId=client_id,
            clientSecret=client_secret,
            startUrl=self.start_url,
        )

        return {
            "verification_uri_complete": auth.get("verificationUriComplete", ""),
            "user_code": auth.get("userCode", ""),
            "device_code": auth["deviceCode"],
            "client_id": client_id,
            "client_secret": client_secret,
            "expires_in": auth.get("expiresIn", 600),
            "interval": auth.get("interval", 5),
        }

    def poll_for_token(
        self, client_id: str, client_secret: str, device_code: str
    ) -> str | None:
        """Poll for token completion.

        Returns:
            The access token string if authorization is complete, None if still pending.

        Raises:
            ClientError: For errors other than AuthorizationPendingException
                         or SlowDownException.
        """
        try:
            token_resp = self._oidc_client.create_token(
                clientId=client_id,
                clientSecret=client_secret,
                grantType="urn:ietf:params:oauth:grant-type:device_code",
                deviceCode=device_code,
            )
            return token_resp["accessToken"]
        except ClientError as e:
            error_code = e.response["Error"]["Code"]
            if error_code in ("AuthorizationPendingException", "SlowDownException"):
                return None
            raise

    def get_identity(self, access_token: str) -> dict:
        """Discover user identity from an SSO access token.

        Walks: ListAccounts -> ListAccountRoles -> GetRoleCredentials -> STS GetCallerIdentity.

        Returns:
            Dict with email, arn, user_id, account_id, account_name,
            role_name, accounts, and roles.
        """
        sso_client = boto3.client("sso", region_name=self.region)

        # List accounts the user has access to
        accounts_resp = sso_client.list_accounts(accessToken=access_token)
        accounts = accounts_resp.get("accountList", [])
        if not accounts:
            return {"error": "No accounts found for this SSO user"}

        first_account = accounts[0]
        account_id = first_account["accountId"]
        account_name = first_account.get("accountName", "")

        # List roles in the first account
        roles_resp = sso_client.list_account_roles(
            accessToken=access_token,
            accountId=account_id,
        )
        roles = roles_resp.get("roleList", [])
        if not roles:
            return {
                "error": "No roles found",
                "accounts": accounts,
            }

        first_role = roles[0]
        role_name = first_role["roleName"]

        # Get temporary credentials for STS call
        creds_resp = sso_client.get_role_credentials(
            accessToken=access_token,
            accountId=account_id,
            roleName=role_name,
        )
        role_creds = creds_resp["roleCredentials"]

        # Call STS to get caller identity
        sts_client = boto3.client(
            "sts",
            aws_access_key_id=role_creds["accessKeyId"],
            aws_secret_access_key=role_creds["secretAccessKey"],
            aws_session_token=role_creds["sessionToken"],
            region_name=self.region,
        )
        identity = sts_client.get_caller_identity()

        email = self._extract_email_from_arn(identity.get("Arn", ""))

        return {
            "email": email,
            "arn": identity.get("Arn", ""),
            "user_id": identity.get("UserId", ""),
            "account_id": account_id,
            "account_name": account_name,
            "role_name": role_name,
            "accounts": [
                {
                    "account_id": a["accountId"],
                    "account_name": a.get("accountName", ""),
                    "email_address": a.get("emailAddress", ""),
                }
                for a in accounts
            ],
            "roles": [
                {"role_name": r["roleName"], "account_id": account_id}
                for r in roles
            ],
        }

    @staticmethod
    def _extract_email_from_arn(arn: str) -> str:
        """Extract email from an SSO assumed-role ARN.

        SSO ARNs typically look like:
            arn:aws:sts::123456789012:assumed-role/AWSReservedSSO_RoleName_hex/user@email.com

        Returns:
            The extracted email, or the last segment of the ARN if no email pattern found.
        """
        # Match the session name (last part after /)
        match = re.search(r"/([^/]+)$", arn)
        if not match:
            return ""
        session_name = match.group(1)
        # Check if it looks like an email
        if "@" in session_name:
            return session_name
        return session_name
