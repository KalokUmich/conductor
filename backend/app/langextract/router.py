"""LangExtract API endpoints.

Provides:
- ``GET /api/langextract/models`` — list available Bedrock models grouped by vendor
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Request

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/langextract", tags=["langextract"])


@router.get("/models")
async def list_models(request: Request) -> dict[str, Any]:
    """Return available Bedrock models grouped by vendor.

    Response shape::

        {
            "region": "eu-west-2",
            "vendors": [
                {
                    "name": "Anthropic",
                    "models": [
                        {"model_id": "anthropic.claude-sonnet-4-6...", "display_name": "...", "vision": true},
                        ...
                    ]
                },
                ...
            ]
        }
    """
    catalog = getattr(request.app.state, "bedrock_catalog", None)
    if catalog is None:
        return {"region": "unknown", "vendors": []}

    vendors_list = []
    for vendor_name, models in sorted(catalog.list_models().items()):
        vendors_list.append({
            "name": vendor_name,
            "models": [m.to_dict() for m in models],
        })

    return {
        "region": catalog.region,
        "vendors": vendors_list,
    }
