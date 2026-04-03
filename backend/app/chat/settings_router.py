"""Room settings REST API router.

Endpoints:
    GET  /rooms/{room_id}/settings - Get room settings
    PUT  /rooms/{room_id}/settings - Update room settings
"""

import logging
from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel

from .manager import manager

logger = logging.getLogger(__name__)

router = APIRouter(tags=["rooms"])


class RoomSettingsResponse(BaseModel):
    """Response model for room settings."""

    code_style: str = ""
    output_mode: str = ""
    strong_model_id: Optional[str] = None
    explorer_model_id: Optional[str] = None


class RoomSettingsUpdate(BaseModel):
    """Request model for updating room settings."""

    code_style: Optional[str] = None
    output_mode: Optional[str] = None
    strong_model_id: Optional[str] = None
    explorer_model_id: Optional[str] = None


@router.get("/rooms/{room_id}/settings", response_model=RoomSettingsResponse)
async def get_room_settings(room_id: str) -> RoomSettingsResponse:
    """Get settings for a room.

    Args:
        room_id: The room ID.

    Returns:
        RoomSettingsResponse with current room settings.
    """
    settings = await manager.ensure_room_settings_loaded(room_id)
    return RoomSettingsResponse(
        code_style=settings.get("code_style", ""),
        output_mode=settings.get("output_mode", ""),
        strong_model_id=settings.get("strong_model_id"),
        explorer_model_id=settings.get("explorer_model_id"),
    )


@router.put("/rooms/{room_id}/settings", response_model=RoomSettingsResponse)
async def update_room_settings(
    room_id: str,
    request: RoomSettingsUpdate,
) -> RoomSettingsResponse:
    """Update settings for a room.

    Args:
        room_id: The room ID.
        request: Settings to update.

    Returns:
        RoomSettingsResponse with updated room settings.
    """
    updates = {}
    if request.code_style is not None:
        updates["code_style"] = request.code_style
    if request.output_mode is not None:
        updates["output_mode"] = request.output_mode
    if request.strong_model_id is not None:
        updates["strong_model_id"] = request.strong_model_id
    if request.explorer_model_id is not None:
        updates["explorer_model_id"] = request.explorer_model_id

    settings = manager.update_room_settings(room_id, updates)
    await manager.persist_room_settings(room_id)
    logger.info(f"Updated room settings for {room_id}")

    return RoomSettingsResponse(
        code_style=settings.get("code_style", ""),
        output_mode=settings.get("output_mode", ""),
        strong_model_id=settings.get("strong_model_id"),
        explorer_model_id=settings.get("explorer_model_id"),
    )
