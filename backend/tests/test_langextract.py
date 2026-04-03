"""Tests for the langextract Bedrock provider, catalog, service, and router."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.langextract.catalog import BedrockCatalog, BedrockModelInfo
from app.langextract.provider import (
    _BEDROCK_MODEL_MAP,
    BedrockLanguageModel,
    ClaudeLanguageModel,
    _call_bedrock,
)
from app.langextract.service import ExtractionResult, LangExtractService

# ---------------------------------------------------------------------------
# BedrockLanguageModel (was ClaudeLanguageModel)
# ---------------------------------------------------------------------------


class TestBedrockLanguageModel:
    def test_default_model(self):
        model = BedrockLanguageModel()
        assert model.model_id == "claude-sonnet-4-20250514"

    def test_custom_model(self):
        model = BedrockLanguageModel(model_id="claude-opus-4-0-20250514")
        assert model.model_id == "claude-opus-4-0-20250514"

    def test_use_bedrock_explicit(self):
        model = BedrockLanguageModel(use_bedrock=True)
        assert model._use_bedrock is True

    def test_use_bedrock_false_explicit(self):
        model = BedrockLanguageModel(use_bedrock=False)
        assert model._use_bedrock is False

    @patch.dict("os.environ", {"AWS_ACCESS_KEY_ID": ""}, clear=False)
    def test_auto_detect_bedrock_from_model_map(self):
        model = BedrockLanguageModel(model_id="claude-sonnet-4-20250514")
        assert model._use_bedrock is True  # model_id is in _BEDROCK_MODEL_MAP

    def test_bedrock_prefix_detected(self):
        model = BedrockLanguageModel(model_id="bedrock/anthropic.claude-sonnet-4-20250514-v1:0")
        assert model._use_bedrock is True

    def test_temperature_stored(self):
        model = BedrockLanguageModel(temperature=0.5)
        assert model.temperature == 0.5

    def test_region_stored(self):
        model = BedrockLanguageModel(region="eu-west-2")
        assert model.region == "eu-west-2"

    def test_catalog_stored(self):
        catalog = BedrockCatalog(region="eu-west-2")
        model = BedrockLanguageModel(catalog=catalog)
        assert model._catalog is catalog

    @patch("app.langextract.provider._call_bedrock")
    def test_infer_bedrock(self, mock_call):
        mock_call.return_value = "Extracted: date=March 15"
        model = BedrockLanguageModel(use_bedrock=True)
        results = list(model.infer(["Extract dates from: deadline March 15"]))
        assert len(results) == 1
        assert results[0][0].output == "Extracted: date=March 15"
        assert results[0][0].score == 1.0

    @patch("app.langextract.provider._call_anthropic_direct")
    def test_infer_direct(self, mock_call):
        mock_call.return_value = "Answer: 42"
        model = BedrockLanguageModel(use_bedrock=False)
        results = list(model.infer(["What is the answer?"]))
        assert len(results) == 1
        assert results[0][0].output == "Answer: 42"

    @patch("app.langextract.provider._call_bedrock")
    def test_infer_batch(self, mock_call):
        mock_call.side_effect = ["Result 1", "Result 2", "Result 3"]
        model = BedrockLanguageModel(use_bedrock=True)
        results = list(model.infer(["p1", "p2", "p3"]))
        assert len(results) == 3
        assert results[0][0].output == "Result 1"
        assert results[2][0].output == "Result 3"

    @patch("app.langextract.provider._call_bedrock")
    def test_infer_error_yields_error_output(self, mock_call):
        mock_call.side_effect = RuntimeError("API down")
        model = BedrockLanguageModel(use_bedrock=True)
        results = list(model.infer(["test"]))
        assert len(results) == 1
        assert results[0][0].score == 0.0
        assert "Error" in results[0][0].output

    @patch("app.langextract.provider._call_bedrock")
    def test_infer_passes_temperature(self, mock_call):
        mock_call.return_value = "ok"
        model = BedrockLanguageModel(use_bedrock=True, temperature=0.7)
        list(model.infer(["test"]))
        _, kwargs = mock_call.call_args
        assert kwargs.get("temperature") == 0.7

    @patch("app.langextract.provider._call_bedrock")
    def test_infer_passes_region(self, mock_call):
        mock_call.return_value = "ok"
        model = BedrockLanguageModel(use_bedrock=True, region="eu-west-2")
        list(model.infer(["test"]))
        _, kwargs = mock_call.call_args
        assert kwargs.get("region") == "eu-west-2"

    @patch("app.langextract.provider._call_bedrock")
    def test_catalog_resolves_inference_profile(self, mock_call):
        mock_call.return_value = "ok"
        catalog = BedrockCatalog(region="eu-west-2")
        catalog._models = [
            BedrockModelInfo(
                model_id="anthropic.claude-haiku-4-5-20251001-v1:0",
                vendor="Anthropic",
                display_name="Claude Haiku 4.5",
                inference_profile="eu.anthropic.claude-haiku-4-5-20251001-v1:0",
            ),
        ]
        catalog._id_to_info = {m.model_id: m for m in catalog._models}

        model = BedrockLanguageModel(
            model_id="anthropic.claude-haiku-4-5-20251001-v1:0",
            use_bedrock=True,
            catalog=catalog,
        )
        list(model.infer(["test"]))
        called_model_id = mock_call.call_args[0][0]
        assert called_model_id == "eu.anthropic.claude-haiku-4-5-20251001-v1:0"

    @patch("app.langextract.provider._call_bedrock")
    def test_static_map_resolves_short_name(self, mock_call):
        mock_call.return_value = "ok"
        model = BedrockLanguageModel(model_id="claude-sonnet-4-20250514", use_bedrock=True)
        list(model.infer(["test"]))
        called_model_id = mock_call.call_args[0][0]
        assert called_model_id == "anthropic.claude-sonnet-4-20250514-v1:0"

    def test_auto_detect_bedrock_from_catalog(self):
        catalog = BedrockCatalog(region="eu-west-2")
        catalog._id_to_info = {"amazon.nova-pro-v1:0": MagicMock()}
        model = BedrockLanguageModel(model_id="amazon.nova-pro-v1:0", catalog=catalog)
        assert model._use_bedrock is True

    @patch("app.langextract.provider._call_bedrock")
    def test_non_claude_model_infer(self, mock_call):
        """Test inference with a non-Claude model (Amazon Nova)."""
        mock_call.return_value = "Nova response"
        model = BedrockLanguageModel(
            model_id="amazon.nova-pro-v1:0",
            use_bedrock=True,
            region="eu-west-2",
        )
        results = list(model.infer(["test"]))
        assert results[0][0].output == "Nova response"
        called_model_id = mock_call.call_args[0][0]
        assert called_model_id == "amazon.nova-pro-v1:0"


# ---------------------------------------------------------------------------
# Backwards compatibility alias
# ---------------------------------------------------------------------------


class TestBackwardsCompatAlias:
    def test_alias_is_same_class(self):
        assert ClaudeLanguageModel is BedrockLanguageModel

    def test_old_import_path_works(self):
        from app.langextract.claude_provider import ClaudeLanguageModel as Old

        assert Old is BedrockLanguageModel

    def test_old_import_bedrock_map(self):
        from app.langextract.claude_provider import _BEDROCK_MODEL_MAP as old_map

        assert old_map is _BEDROCK_MODEL_MAP


# ---------------------------------------------------------------------------
# Provider registration
# ---------------------------------------------------------------------------


class TestProviderRegistration:
    def test_provider_registered(self):
        from langextract.providers import router

        import app.langextract.provider  # noqa: F401

        providers = router.list_providers()
        patterns_flat = [p for group, _ in providers for p in group]
        assert any("claude" in p for p in patterns_flat)

    def test_resolve_claude_model(self):
        from langextract.providers import router

        import app.langextract.provider  # noqa: F401

        cls = router.resolve("claude-sonnet-4-20250514")
        assert cls is not None
        assert issubclass(cls, BedrockLanguageModel)

    def test_resolve_amazon_model(self):
        from langextract.providers import router

        import app.langextract.provider  # noqa: F401

        cls = router.resolve("amazon.nova-pro-v1:0")
        assert cls is not None
        assert issubclass(cls, BedrockLanguageModel)

    def test_resolve_meta_model(self):
        from langextract.providers import router

        import app.langextract.provider  # noqa: F401

        cls = router.resolve("meta.llama3-70b-instruct-v1:0")
        assert cls is not None
        assert issubclass(cls, BedrockLanguageModel)

    def test_resolve_mistral_model(self):
        from langextract.providers import router

        import app.langextract.provider  # noqa: F401

        cls = router.resolve("mistral.mistral-large-2402-v1:0")
        assert cls is not None
        assert issubclass(cls, BedrockLanguageModel)

    def test_resolve_deepseek_model(self):
        from langextract.providers import router

        import app.langextract.provider  # noqa: F401

        cls = router.resolve("deepseek.deepseek-r1-v1:0")
        assert cls is not None
        assert issubclass(cls, BedrockLanguageModel)

    def test_resolve_bedrock_prefix(self):
        from langextract.providers import router

        import app.langextract.provider  # noqa: F401

        cls = router.resolve("bedrock/amazon.nova-pro-v1:0")
        assert cls is not None
        assert issubclass(cls, BedrockLanguageModel)


# ---------------------------------------------------------------------------
# Bedrock model ID mapping
# ---------------------------------------------------------------------------


class TestBedrockModelMap:
    def test_known_models_mapped(self):
        assert "claude-sonnet-4-20250514" in _BEDROCK_MODEL_MAP
        assert "claude-opus-4-0-20250514" in _BEDROCK_MODEL_MAP

    def test_mapped_ids_contain_anthropic(self):
        for _, bedrock_id in _BEDROCK_MODEL_MAP.items():
            assert "anthropic" in bedrock_id


# ---------------------------------------------------------------------------
# _call_bedrock region parameter
# ---------------------------------------------------------------------------


class TestCallBedrockRegion:
    @patch("app.langextract.provider.boto3")
    def test_region_passed_to_client(self, mock_boto3):
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client
        mock_client.converse.return_value = {
            "output": {"message": {"content": [{"text": "hello"}]}},
        }
        result = _call_bedrock("some-model", "prompt", region="eu-west-2")
        mock_boto3.client.assert_called_once_with("bedrock-runtime", region_name="eu-west-2")
        assert result == "hello"

    @patch("app.langextract.provider.boto3")
    @patch.dict("os.environ", {"AWS_DEFAULT_REGION": "ap-southeast-1"}, clear=False)
    def test_region_fallback_to_env(self, mock_boto3):
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client
        mock_client.converse.return_value = {
            "output": {"message": {"content": [{"text": "ok"}]}},
        }
        _call_bedrock("some-model", "prompt")
        mock_boto3.client.assert_called_once_with("bedrock-runtime", region_name="ap-southeast-1")


# ---------------------------------------------------------------------------
# BedrockCatalog
# ---------------------------------------------------------------------------


_MOCK_FM_RESPONSE = {
    "modelSummaries": [
        {
            "modelId": "anthropic.claude-sonnet-4-6-20260514-v1:0",
            "providerName": "Anthropic",
            "modelName": "Claude Sonnet 4.6",
            "inputModalities": ["TEXT", "IMAGE"],
            "outputModalities": ["TEXT"],
            "inferenceTypesSupported": ["ON_DEMAND"],
            "modelLifecycle": {"status": "ACTIVE"},
        },
        {
            "modelId": "anthropic.claude-haiku-4-5-20251001-v1:0",
            "providerName": "Anthropic",
            "modelName": "Claude Haiku 4.5",
            "inputModalities": ["TEXT", "IMAGE"],
            "outputModalities": ["TEXT"],
            "inferenceTypesSupported": ["INFERENCE_PROFILE"],
            "modelLifecycle": {"status": "ACTIVE"},
        },
        {
            "modelId": "anthropic.claude-opus-4-6-20260514-v1:0",
            "providerName": "Anthropic",
            "modelName": "Claude Opus 4.6",
            "inputModalities": ["TEXT", "IMAGE"],
            "outputModalities": ["TEXT"],
            "inferenceTypesSupported": ["INFERENCE_PROFILE"],
            "modelLifecycle": {"status": "ACTIVE"},
        },
        {
            "modelId": "amazon.nova-pro-v1:0",
            "providerName": "Amazon",
            "modelName": "Nova Pro",
            "inputModalities": ["TEXT", "IMAGE"],
            "outputModalities": ["TEXT"],
            "inferenceTypesSupported": ["ON_DEMAND"],
            "modelLifecycle": {"status": "ACTIVE"},
        },
        {
            "modelId": "meta.llama3-70b-instruct-v1:0",
            "providerName": "Meta",
            "modelName": "Llama 3 70B Instruct",
            "inputModalities": ["TEXT"],
            "outputModalities": ["TEXT"],
            "inferenceTypesSupported": ["ON_DEMAND"],
            "modelLifecycle": {"status": "ACTIVE"},
        },
        {
            "modelId": "mistral.mistral-large-2402-v1:0",
            "providerName": "Mistral AI",
            "modelName": "Mistral Large",
            "inputModalities": ["TEXT"],
            "outputModalities": ["TEXT"],
            "inferenceTypesSupported": ["ON_DEMAND"],
            "modelLifecycle": {"status": "ACTIVE"},
        },
        {
            # Image-only model — should be excluded (no TEXT output)
            "modelId": "stability.stable-diffusion-xl-v1:0",
            "providerName": "Stability AI",
            "modelName": "SDXL",
            "inputModalities": ["TEXT"],
            "outputModalities": ["IMAGE"],
            "inferenceTypesSupported": ["ON_DEMAND"],
            "modelLifecycle": {"status": "ACTIVE"},
        },
        {
            # Legacy model — should be excluded (not ACTIVE)
            "modelId": "anthropic.claude-v1",
            "providerName": "Anthropic",
            "modelName": "Claude v1",
            "inputModalities": ["TEXT"],
            "outputModalities": ["TEXT"],
            "inferenceTypesSupported": ["ON_DEMAND"],
            "modelLifecycle": {"status": "LEGACY"},
        },
        {
            # INFERENCE_PROFILE only, no profile exists — should be excluded
            "modelId": "amazon.nova-2-lite-v1:0",
            "providerName": "Amazon",
            "modelName": "Nova 2 Lite",
            "inputModalities": ["TEXT"],
            "outputModalities": ["TEXT"],
            "inferenceTypesSupported": ["INFERENCE_PROFILE"],
            "modelLifecycle": {"status": "ACTIVE"},
        },
    ]
}

_MOCK_IP_RESPONSE = {
    "inferenceProfileSummaries": [
        {
            "inferenceProfileId": "eu.anthropic.claude-haiku-4-5-20251001-v1:0",
            "models": [
                {"modelArn": "arn:aws:bedrock:eu-west-2::foundation-model/anthropic.claude-haiku-4-5-20251001-v1:0"},
            ],
        },
    ]
}


class TestBedrockCatalog:
    def _make_mock_bedrock_client(self):
        client = MagicMock()
        client.list_foundation_models.return_value = _MOCK_FM_RESPONSE
        client.list_inference_profiles.return_value = _MOCK_IP_RESPONSE
        # Make paginator raise so fallback to list_inference_profiles is used
        client.get_paginator.side_effect = Exception("no paginator")
        return client

    @patch("app.langextract.catalog.boto3")
    def test_refresh_populates_models(self, mock_boto3):
        mock_boto3.client.return_value = self._make_mock_bedrock_client()
        catalog = BedrockCatalog(region="eu-west-2")
        catalog.refresh()

        all_models = catalog.get_all_models()
        # Expect: sonnet-4-6 (on-demand), haiku-4-5 (profile exists),
        #         nova-pro (on-demand), llama3 (on-demand), mistral-large (on-demand)
        # Excluded: sdxl (no TEXT output), claude-v1 (LEGACY),
        #           claude-opus-4-6 (INFERENCE_PROFILE only, no profile),
        #           nova-2-lite (INFERENCE_PROFILE only, no profile)
        assert len(all_models) == 5

    @patch("app.langextract.catalog.boto3")
    def test_refresh_groups_by_vendor(self, mock_boto3):
        mock_boto3.client.return_value = self._make_mock_bedrock_client()
        catalog = BedrockCatalog(region="eu-west-2")
        catalog.refresh()

        by_vendor = catalog.list_models()
        assert "Anthropic" in by_vendor
        assert "Amazon" in by_vendor
        assert "Meta" in by_vendor
        assert "Mistral AI" in by_vendor
        assert len(by_vendor["Anthropic"]) == 2  # sonnet + haiku

    @patch("app.langextract.catalog.boto3")
    def test_on_demand_model_no_profile(self, mock_boto3):
        mock_boto3.client.return_value = self._make_mock_bedrock_client()
        catalog = BedrockCatalog(region="eu-west-2")
        catalog.refresh()

        # On-demand models should not have inference_profile set
        nova = [m for m in catalog.get_all_models() if m.model_id == "amazon.nova-pro-v1:0"]
        assert len(nova) == 1
        assert nova[0].on_demand is True
        assert nova[0].inference_profile is None

    @patch("app.langextract.catalog.boto3")
    def test_inference_profile_model(self, mock_boto3):
        mock_boto3.client.return_value = self._make_mock_bedrock_client()
        catalog = BedrockCatalog(region="eu-west-2")
        catalog.refresh()

        haiku = [m for m in catalog.get_all_models() if "haiku" in m.model_id]
        assert len(haiku) == 1
        assert haiku[0].on_demand is False
        assert haiku[0].inference_profile == "eu.anthropic.claude-haiku-4-5-20251001-v1:0"

    @patch("app.langextract.catalog.boto3")
    def test_get_effective_model_id_on_demand(self, mock_boto3):
        mock_boto3.client.return_value = self._make_mock_bedrock_client()
        catalog = BedrockCatalog(region="eu-west-2")
        catalog.refresh()

        # On-demand model returns itself
        assert catalog.get_effective_model_id("amazon.nova-pro-v1:0") == "amazon.nova-pro-v1:0"

    @patch("app.langextract.catalog.boto3")
    def test_get_effective_model_id_with_profile(self, mock_boto3):
        mock_boto3.client.return_value = self._make_mock_bedrock_client()
        catalog = BedrockCatalog(region="eu-west-2")
        catalog.refresh()

        # Profile model returns the profile ID
        assert (
            catalog.get_effective_model_id("anthropic.claude-haiku-4-5-20251001-v1:0")
            == "eu.anthropic.claude-haiku-4-5-20251001-v1:0"
        )

    @patch("app.langextract.catalog.boto3")
    def test_get_effective_model_id_unknown(self, mock_boto3):
        mock_boto3.client.return_value = self._make_mock_bedrock_client()
        catalog = BedrockCatalog(region="eu-west-2")
        catalog.refresh()

        # Unknown model returns itself unchanged
        assert catalog.get_effective_model_id("unknown-model") == "unknown-model"

    @patch("app.langextract.catalog.boto3")
    def test_has_model(self, mock_boto3):
        mock_boto3.client.return_value = self._make_mock_bedrock_client()
        catalog = BedrockCatalog(region="eu-west-2")
        catalog.refresh()

        assert catalog.has_model("amazon.nova-pro-v1:0") is True
        assert catalog.has_model("nonexistent-model") is False

    @patch("app.langextract.catalog.boto3")
    def test_vision_flag(self, mock_boto3):
        mock_boto3.client.return_value = self._make_mock_bedrock_client()
        catalog = BedrockCatalog(region="eu-west-2")
        catalog.refresh()

        sonnet = next(m for m in catalog.get_all_models() if "sonnet" in m.model_id)
        assert sonnet.vision is True

        llama = next(m for m in catalog.get_all_models() if "llama" in m.model_id)
        assert llama.vision is False

    @patch("app.langextract.catalog.boto3")
    def test_excludes_inactive_models(self, mock_boto3):
        mock_boto3.client.return_value = self._make_mock_bedrock_client()
        catalog = BedrockCatalog(region="eu-west-2")
        catalog.refresh()

        ids = [m.model_id for m in catalog.get_all_models()]
        assert "anthropic.claude-v1" not in ids

    @patch("app.langextract.catalog.boto3")
    def test_excludes_non_text_output(self, mock_boto3):
        mock_boto3.client.return_value = self._make_mock_bedrock_client()
        catalog = BedrockCatalog(region="eu-west-2")
        catalog.refresh()

        ids = [m.model_id for m in catalog.get_all_models()]
        assert "stability.stable-diffusion-xl-v1:0" not in ids

    @patch("app.langextract.catalog.boto3")
    def test_excludes_inference_profile_without_profile(self, mock_boto3):
        mock_boto3.client.return_value = self._make_mock_bedrock_client()
        catalog = BedrockCatalog(region="eu-west-2")
        catalog.refresh()

        ids = [m.model_id for m in catalog.get_all_models()]
        # opus-4-6 has INFERENCE_PROFILE only but no profile in the mock
        assert "anthropic.claude-opus-4-6-20260514-v1:0" not in ids
        # nova-2-lite has INFERENCE_PROFILE only but no profile in the mock
        assert "amazon.nova-2-lite-v1:0" not in ids

    @patch("app.langextract.catalog.boto3")
    def test_to_dict(self, mock_boto3):
        mock_boto3.client.return_value = self._make_mock_bedrock_client()
        catalog = BedrockCatalog(region="eu-west-2")
        catalog.refresh()

        haiku = next(m for m in catalog.get_all_models() if "haiku" in m.model_id)
        d = haiku.to_dict()
        assert "model_id" in d
        assert "vendor" in d
        assert "display_name" in d
        assert "vision" in d
        assert "inference_profile" in d  # has profile

        nova = next(m for m in catalog.get_all_models() if "nova-pro" in m.model_id)
        d2 = nova.to_dict()
        assert "inference_profile" not in d2  # on-demand, no profile

    @patch("app.langextract.catalog.boto3")
    def test_refresh_with_paginator(self, mock_boto3):
        """Test that paginator path works when available."""
        client = MagicMock()
        client.list_foundation_models.return_value = _MOCK_FM_RESPONSE

        paginator = MagicMock()
        paginator.paginate.return_value = [_MOCK_IP_RESPONSE]
        client.get_paginator.return_value = paginator

        mock_boto3.client.return_value = client
        catalog = BedrockCatalog(region="eu-west-2")
        catalog.refresh()

        # Should still work with paginator
        all_models = catalog.get_all_models()
        assert len(all_models) == 5


# ---------------------------------------------------------------------------
# LangExtractService
# ---------------------------------------------------------------------------


class TestLangExtractService:
    def test_init_defaults(self):
        svc = LangExtractService()
        assert svc.model_id == "claude-sonnet-4-20250514"
        assert svc.use_bedrock is None
        assert svc.temperature is None
        assert svc.region is None
        assert svc._catalog is None

    def test_init_custom(self):
        svc = LangExtractService(
            model_id="claude-opus-4-0-20250514",
            use_bedrock=True,
            temperature=0.3,
            region="eu-west-2",
        )
        assert svc.model_id == "claude-opus-4-0-20250514"
        assert svc.use_bedrock is True
        assert svc.temperature == 0.3
        assert svc.region == "eu-west-2"

    def test_init_with_catalog(self):
        catalog = BedrockCatalog(region="eu-west-2")
        svc = LangExtractService(catalog=catalog)
        assert svc._catalog is catalog

    def test_list_available_models_no_catalog(self):
        svc = LangExtractService()
        assert svc.list_available_models() == {}

    def test_list_available_models_with_catalog(self):
        catalog = BedrockCatalog(region="eu-west-2")
        catalog._by_vendor = {
            "Anthropic": [
                BedrockModelInfo(model_id="anthropic.claude-sonnet-4-6", vendor="Anthropic", display_name="Sonnet 4.6"),
            ],
            "Amazon": [
                BedrockModelInfo(model_id="amazon.nova-pro-v1:0", vendor="Amazon", display_name="Nova Pro"),
            ],
        }
        svc = LangExtractService(catalog=catalog)
        result = svc.list_available_models()
        assert "Anthropic" in result
        assert "Amazon" in result

    @pytest.mark.asyncio
    async def test_extract_requires_examples(self):
        svc = LangExtractService(use_bedrock=True)
        result = await svc.extract_from_text(
            text="Alice said the deadline is March 15.",
            prompt="Extract people and dates.",
        )
        assert not result.success
        assert "examples are required" in result.error

    @pytest.mark.asyncio
    @patch("langextract.extract")
    async def test_extract_from_text_with_examples(self, mock_extract):
        from langextract.data import AnnotatedDocument

        mock_doc = MagicMock(spec=AnnotatedDocument)
        mock_extract.return_value = [mock_doc]

        svc = LangExtractService(use_bedrock=True)

        from langextract.data import ExampleData, Extraction

        examples = [
            ExampleData(
                text="Bob mentioned June 1.",
                extractions=[
                    Extraction(extraction_class="Person", extraction_text="Bob"),
                    Extraction(extraction_class="Date", extraction_text="June 1"),
                ],
            )
        ]

        result = await svc.extract_from_text(
            text="Alice said the deadline is March 15.",
            prompt="Extract people and dates.",
            examples=examples,
        )
        assert result.success
        assert len(result.documents) == 1

    @pytest.mark.asyncio
    async def test_extract_from_text_error(self):
        svc = LangExtractService(use_bedrock=False)
        from langextract.data import ExampleData, Extraction

        examples = [
            ExampleData(
                text="test input",
                extractions=[Extraction(extraction_class="Entity", extraction_text="test")],
            )
        ]
        with patch("app.langextract.provider._call_anthropic_direct", side_effect=RuntimeError("No key")):
            result = await svc.extract_from_text(
                text="test",
                prompt="extract",
                examples=examples,
            )
            assert isinstance(result, ExtractionResult)


# ---------------------------------------------------------------------------
# Router endpoint
# ---------------------------------------------------------------------------


class TestLangExtractRouter:
    def test_models_endpoint_no_catalog(self, api_client):
        response = api_client.get("/api/langextract/models")
        assert response.status_code == 200
        data = response.json()
        assert data["region"] == "unknown"
        assert data["vendors"] == []

    def test_models_endpoint_with_catalog(self, api_client):
        catalog = BedrockCatalog(region="eu-west-2")
        catalog._by_vendor = {
            "Anthropic": [
                BedrockModelInfo(
                    model_id="anthropic.claude-sonnet-4-6",
                    vendor="Anthropic",
                    display_name="Claude Sonnet 4.6",
                    vision=True,
                    on_demand=True,
                ),
            ],
            "Amazon": [
                BedrockModelInfo(
                    model_id="amazon.nova-pro-v1:0",
                    vendor="Amazon",
                    display_name="Nova Pro",
                    vision=True,
                    on_demand=True,
                ),
            ],
        }

        from app.main import app

        app.state.bedrock_catalog = catalog
        try:
            response = api_client.get("/api/langextract/models")
            assert response.status_code == 200
            data = response.json()
            assert data["region"] == "eu-west-2"
            assert len(data["vendors"]) == 2
            vendor_names = [v["name"] for v in data["vendors"]]
            assert "Amazon" in vendor_names
            assert "Anthropic" in vendor_names
            # Check model structure
            anthropic_vendor = next(v for v in data["vendors"] if v["name"] == "Anthropic")
            assert len(anthropic_vendor["models"]) == 1
            assert anthropic_vendor["models"][0]["model_id"] == "anthropic.claude-sonnet-4-6"
            assert anthropic_vendor["models"][0]["vision"] is True
        finally:
            app.state.bedrock_catalog = None
