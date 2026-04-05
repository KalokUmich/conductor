"""Auth router for SSO login endpoints (AWS SSO + Google OAuth).

Endpoints:
    POST /auth/sso/start     - Start AWS SSO device authorization flow
    POST /auth/sso/poll       - Poll for AWS SSO token and resolve identity
    POST /auth/google/start   - Start Google OAuth device authorization flow
    POST /auth/google/poll    - Poll for Google OAuth token and resolve identity
    GET  /auth/providers      - List enabled auth providers
"""

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.config import get_config

from .google_service import GoogleSSOService
from .service import SSOService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


async def _poll_for_identity(
    poll_fn,
    get_identity_fn,
    provider_label: str,
) -> dict:
    """Shared poll logic for SSO device authorization flows.

    Args:
        poll_fn: Callable that returns an access token string or None if pending.
        get_identity_fn: Callable that takes an access token and returns identity dict.
        provider_label: Label for logging (e.g., "SSO", "Google SSO").

    Returns:
        dict with status (pending, complete, expired, error) and optional identity.
    """
    try:
        access_token = poll_fn()

        if access_token is None:
            return {"status": "pending"}

        identity = get_identity_fn(access_token)

        # Create or fetch persistent user profile
        user_profile = None
        try:
            from .user_service import UserService
            user_svc = UserService.get_instance()
            email = identity.get("email") or identity.get("arn", "")
            if email:
                user_profile = await user_svc.get_or_create_user(
                    email=email,
                    display_name=identity.get("name") or identity.get("given_name"),
                    auth_provider=provider_label.lower().replace(" ", "_"),
                )
        except Exception as e:
            logger.warning(f"[Auth] User profile creation failed: {e}")

        return {
            "status": "complete",
            "identity": identity,
            "userUuid": user_profile["id"] if user_profile else None,
            "userProfile": user_profile,
        }
    except Exception as e:
        error_msg = str(e)
        logger.error(f"{provider_label} poll failed: {error_msg}")

        if "ExpiredTokenException" in error_msg or "expired" in error_msg.lower():
            return {"status": "expired", "error": error_msg}

        return {"status": "error", "error": error_msg}


class SSOPollRequest(BaseModel):
    """Request body for polling SSO token status."""

    device_code: str
    client_id: str
    client_secret: str


@router.post("/sso/start")
async def sso_start() -> dict:
    """Start the SSO OIDC device authorization flow.

    Reads SSO config from settings. Returns verification URL,
    user code, device code, and client credentials for polling.
    """
    config = get_config()
    if not config.sso.enabled:
        raise HTTPException(status_code=400, detail="SSO is not enabled")
    if not config.sso.start_url:
        raise HTTPException(status_code=400, detail="SSO start_url is not configured")

    try:
        service = SSOService(
            start_url=config.sso.start_url,
            region=config.sso.region,
        )
        result = service.register_and_start()
        return result
    except Exception as e:
        logger.error(f"SSO start failed: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/sso/poll")
async def sso_poll(request: SSOPollRequest) -> dict:
    """Poll for SSO token completion and resolve identity.

    Returns status: pending, complete, expired, or error.
    When complete, includes the user's identity information.
    """
    config = get_config()
    if not config.sso.enabled:
        raise HTTPException(status_code=400, detail="SSO is not enabled")

    service = SSOService(
        start_url=config.sso.start_url,
        region=config.sso.region,
    )
    return await _poll_for_identity(
        poll_fn=lambda: service.poll_for_token(
            client_id=request.client_id,
            client_secret=request.client_secret,
            device_code=request.device_code,
        ),
        get_identity_fn=service.get_identity,
        provider_label="SSO",
    )


# =============================================================================
# Google OAuth SSO Endpoints
# =============================================================================


class GooglePollRequest(BaseModel):
    """Request body for polling Google OAuth token status."""

    device_code: str


@router.post("/google/start")
async def google_start() -> dict:
    """Start Google OAuth 2.0 device authorization flow.

    Reads Google SSO config from settings and secrets.
    Returns verification URL, user code, device code, and interval.
    """
    config = get_config()
    if not config.google_sso.enabled:
        raise HTTPException(status_code=400, detail="Google SSO is not enabled")
    if not config.google_sso_secrets.client_id:
        raise HTTPException(status_code=400, detail="Google SSO client_id is not configured")

    try:
        service = GoogleSSOService(
            client_id=config.google_sso_secrets.client_id,
            client_secret=config.google_sso_secrets.client_secret,
        )
        result = service.start_device_flow()
        return result
    except Exception as e:
        logger.error(f"Google SSO start failed: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/google/poll")
async def google_poll(request: GooglePollRequest) -> dict:
    """Poll for Google OAuth token completion and resolve identity.

    Returns status: pending, complete, expired, or error.
    When complete, includes the user's identity information.
    """
    config = get_config()
    if not config.google_sso.enabled:
        raise HTTPException(status_code=400, detail="Google SSO is not enabled")

    service = GoogleSSOService(
        client_id=config.google_sso_secrets.client_id,
        client_secret=config.google_sso_secrets.client_secret,
    )
    return await _poll_for_identity(
        poll_fn=lambda: service.poll_for_token(device_code=request.device_code),
        get_identity_fn=service.get_identity,
        provider_label="Google SSO",
    )


# =============================================================================
# Provider Discovery Endpoint
# =============================================================================


@router.get("/providers")
async def auth_providers() -> dict:
    """List authentication providers that are both enabled and properly configured.

    A provider is only reported as available when its enabled flag is true
    AND the required credentials/config are present.
    """
    config = get_config()
    return {
        "aws": config.sso.enabled and bool(config.sso.start_url),
        "google": config.google_sso.enabled and bool(config.google_sso_secrets.client_id),
    }
