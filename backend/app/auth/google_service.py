"""Google OAuth 2.0 device authorization flow service.

Implements the same device-flow pattern as the AWS SSO service:
1. Start device authorization (user gets a verification URL + code)
2. Poll for token completion
3. Use the access token to fetch user identity from Google's userinfo endpoint
"""

import logging

import httpx

logger = logging.getLogger(__name__)


class GoogleSSOService:
    """Handles Google OAuth 2.0 device authorization and identity resolution."""

    DEVICE_CODE_URL = "https://oauth2.googleapis.com/device/code"
    TOKEN_URL = "https://oauth2.googleapis.com/token"
    USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"
    SCOPES = "openid email profile"

    def __init__(self, client_id: str, client_secret: str):
        self.client_id = client_id
        self.client_secret = client_secret

    def start_device_flow(self) -> dict:
        """Start the device authorization flow.

        Returns:
            Dict with device_code, user_code, verification_url, expires_in, interval.
        """
        resp = httpx.post(
            self.DEVICE_CODE_URL,
            data={
                "client_id": self.client_id,
                "scope": self.SCOPES,
            },
        )
        resp.raise_for_status()
        data = resp.json()

        return {
            "device_code": data["device_code"],
            "user_code": data["user_code"],
            "verification_url": data.get("verification_url", ""),
            "expires_in": data.get("expires_in", 1800),
            "interval": data.get("interval", 5),
        }

    def poll_for_token(self, device_code: str) -> str | None:
        """Poll for token completion.

        Returns:
            The access token string if authorization is complete, None if still pending.

        Raises:
            RuntimeError: For errors other than authorization_pending or slow_down.
        """
        resp = httpx.post(
            self.TOKEN_URL,
            data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "device_code": device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            },
        )
        data = resp.json()

        if "access_token" in data:
            return data["access_token"]

        error = data.get("error", "")
        if error in ("authorization_pending", "slow_down"):
            return None

        # Terminal error
        error_desc = data.get("error_description", error)
        raise RuntimeError(f"Google token error: {error_desc}")

    def get_identity(self, access_token: str) -> dict:
        """Fetch user identity from Google's userinfo endpoint.

        Returns:
            Dict with email, name, picture, and id.
        """
        resp = httpx.get(
            self.USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        resp.raise_for_status()
        data = resp.json()

        return {
            "email": data.get("email", ""),
            "name": data.get("name", ""),
            "picture": data.get("picture", ""),
            "id": data.get("id", ""),
        }
