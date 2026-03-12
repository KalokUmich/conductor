"""Dynamic Bedrock model catalog populated from AWS Bedrock APIs.

At startup, calls ``list_foundation_models()`` and ``list_inference_profiles()``
to discover all available text models in the configured region.  Models are
grouped by vendor for UI consumption.

Usage::

    catalog = BedrockCatalog(region="eu-west-2")
    catalog.refresh()
    models_by_vendor = catalog.list_models()
    effective_id = catalog.get_effective_model_id("anthropic.claude-haiku-4-5-20251001-v1:0")
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

import boto3

logger = logging.getLogger(__name__)


@dataclass
class BedrockModelInfo:
    """Metadata for a single Bedrock model."""

    model_id: str
    vendor: str
    display_name: str
    vision: bool = False
    on_demand: bool = False
    inference_profile: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "model_id": self.model_id,
            "vendor": self.vendor,
            "display_name": self.display_name,
            "vision": self.vision,
        }
        if self.inference_profile:
            d["inference_profile"] = self.inference_profile
        return d


class BedrockCatalog:
    """Dynamic model catalog populated from Bedrock APIs at startup."""

    def __init__(
        self,
        region: str = "eu-west-2",
        access_key_id: str | None = None,
        secret_access_key: str | None = None,
        session_token: str | None = None,
    ) -> None:
        self.region = region
        self._access_key_id = access_key_id
        self._secret_access_key = secret_access_key
        self._session_token = session_token
        self._models: list[BedrockModelInfo] = []
        self._by_vendor: dict[str, list[BedrockModelInfo]] = {}
        self._id_to_info: dict[str, BedrockModelInfo] = {}

    def refresh(self) -> None:
        """Call Bedrock APIs to populate the catalog.

        1. ``list_foundation_models()`` — filter TEXT input+output, ACTIVE
        2. ``list_inference_profiles()`` — build ``{base_model_id -> profile_id}`` map
        3. For each model:
           - ON_DEMAND supported → usable directly
           - Only INFERENCE_PROFILE → check if a profile exists → use profile ID
           - Neither → skip
        """
        kwargs: dict = {"region_name": self.region}
        if self._access_key_id:
            kwargs["aws_access_key_id"] = self._access_key_id
        if self._secret_access_key:
            kwargs["aws_secret_access_key"] = self._secret_access_key
        if self._session_token:
            kwargs["aws_session_token"] = self._session_token
        bedrock = boto3.client("bedrock", **kwargs)

        # 1. Foundation models
        fm_response = bedrock.list_foundation_models()
        all_models = fm_response.get("modelSummaries", [])

        # 2. Inference profiles
        profiles: dict[str, str] = {}
        try:
            paginator = bedrock.get_paginator("list_inference_profiles")
            for page in paginator.paginate():
                for prof in page.get("inferenceProfileSummaries", []):
                    profile_id = prof.get("inferenceProfileId", "")
                    for m in prof.get("models", []):
                        base_id = m.get("modelArn", "").split("/")[-1]
                        if base_id:
                            profiles[base_id] = profile_id
        except Exception:
            # list_inference_profiles may not support pagination in all SDKs
            try:
                ip_response = bedrock.list_inference_profiles()
                for prof in ip_response.get("inferenceProfileSummaries", []):
                    profile_id = prof.get("inferenceProfileId", "")
                    for m in prof.get("models", []):
                        base_id = m.get("modelArn", "").split("/")[-1]
                        if base_id:
                            profiles[base_id] = profile_id
            except Exception as exc:
                logger.warning("Could not list inference profiles: %s", exc)

        # 3. Build catalog
        models: list[BedrockModelInfo] = []
        for fm in all_models:
            status = fm.get("modelLifecycle", {}).get("status", "")
            if status != "ACTIVE":
                continue

            input_modalities = fm.get("inputModalities", [])
            output_modalities = fm.get("outputModalities", [])
            if "TEXT" not in input_modalities or "TEXT" not in output_modalities:
                continue

            model_id = fm.get("modelId", "")
            vendor = fm.get("providerName", "Unknown")
            display_name = fm.get("modelName", model_id)
            vision = "IMAGE" in input_modalities
            inference_types = fm.get("inferenceTypesSupported", [])

            on_demand = "ON_DEMAND" in inference_types
            has_profile = model_id in profiles

            if not on_demand and not has_profile:
                continue

            info = BedrockModelInfo(
                model_id=model_id,
                vendor=vendor,
                display_name=display_name,
                vision=vision,
                on_demand=on_demand,
                inference_profile=profiles.get(model_id) if not on_demand else None,
            )
            models.append(info)

        self._models = models
        by_vendor: dict[str, list[BedrockModelInfo]] = defaultdict(list)
        id_map: dict[str, BedrockModelInfo] = {}
        for m in models:
            by_vendor[m.vendor].append(m)
            id_map[m.model_id] = m
        self._by_vendor = dict(by_vendor)
        self._id_to_info = id_map

        logger.info(
            "Bedrock catalog: %d models from %d vendors in %s",
            len(self._models),
            len(self._by_vendor),
            self.region,
        )

    def get_effective_model_id(self, model_id: str) -> str:
        """Return the ID to pass to the Converse API.

        If the model requires an inference profile, returns the profile ID.
        Otherwise returns the model_id unchanged.
        """
        info = self._id_to_info.get(model_id)
        if info and info.inference_profile:
            return info.inference_profile
        return model_id

    def list_models(self) -> dict[str, list[BedrockModelInfo]]:
        """Return ``{vendor: [models]}`` for the UI."""
        return dict(self._by_vendor)

    def get_all_models(self) -> list[BedrockModelInfo]:
        """Return flat list of all available models."""
        return list(self._models)

    def has_model(self, model_id: str) -> bool:
        """Check if a model ID is in the catalog."""
        return model_id in self._id_to_info
