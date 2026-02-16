"""Auth router for AWS SSO login endpoints.

Endpoints:
    POST /auth/sso/start - Start SSO device authorization flow
    POST /auth/sso/poll  - Poll for token and resolve identity
"""
import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.config import get_config

from .service import SSOService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


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
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/sso/poll")
async def sso_poll(request: SSOPollRequest) -> dict:
    """Poll for SSO token completion and resolve identity.

    Returns status: pending, complete, expired, or error.
    When complete, includes the user's identity information.
    """
    config = get_config()
    if not config.sso.enabled:
        raise HTTPException(status_code=400, detail="SSO is not enabled")

    try:
        service = SSOService(
            start_url=config.sso.start_url,
            region=config.sso.region,
        )

        access_token = service.poll_for_token(
            client_id=request.client_id,
            client_secret=request.client_secret,
            device_code=request.device_code,
        )

        if access_token is None:
            return {"status": "pending"}

        # Token obtained — resolve identity
        identity = service.get_identity(access_token)
        return {
            "status": "complete",
            "identity": identity,
        }
    except Exception as e:
        error_msg = str(e)
        logger.error(f"SSO poll failed: {error_msg}")

        if "ExpiredTokenException" in error_msg or "expired" in error_msg.lower():
            return {"status": "expired", "error": error_msg}

        return {"status": "error", "error": error_msg}
