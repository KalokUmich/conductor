"""Tests for AIProvider interface and implementations."""
import json
from unittest.mock import MagicMock, patch

import pytest

from app.ai_provider import (
    AIProvider,
    ChatMessage,
    ClaudeBedrockProvider,
    ClaudeDirectProvider,
    DecisionSummary,
)


class TestAIProviderInterface:
    """Tests for the AIProvider abstract interface."""

    def test_cannot_instantiate_abstract_class(self):
        """AIProvider should not be directly instantiable."""
        with pytest.raises(TypeError):
            AIProvider()

    def test_interface_has_required_methods(self):
        """AIProvider should define health_check, summarize, and summarize_structured methods."""
        assert hasattr(AIProvider, "health_check")
        assert hasattr(AIProvider, "summarize")
        assert hasattr(AIProvider, "summarize_structured")


class TestClaudeDirectProvider:
    """Tests for ClaudeDirectProvider implementation."""

    def test_initialization_with_defaults(self):
        """Test provider initialization with default values."""
        provider = ClaudeDirectProvider(api_key="test-key")
        assert provider.api_key == "test-key"
        assert provider.model == ClaudeDirectProvider.DEFAULT_MODEL
        assert provider.base_url == ClaudeDirectProvider.DEFAULT_BASE_URL

    def test_initialization_with_custom_values(self):
        """Test provider initialization with custom values."""
        provider = ClaudeDirectProvider(
            api_key="custom-key",
            model="claude-3-opus-20240229",
            base_url="https://custom.api.com"
        )
        assert provider.api_key == "custom-key"
        assert provider.model == "claude-3-opus-20240229"
        assert provider.base_url == "https://custom.api.com"

    def test_implements_ai_provider_interface(self):
        """ClaudeDirectProvider should implement AIProvider interface."""
        provider = ClaudeDirectProvider(api_key="test-key")
        assert isinstance(provider, AIProvider)

    def test_health_check_success(self):
        """Test health_check returns True when API is accessible."""
        mock_anthropic = MagicMock()
        mock_client = MagicMock()
        mock_anthropic.Anthropic.return_value = mock_client
        mock_client.messages.create.return_value = MagicMock()

        with patch.dict("sys.modules", {"anthropic": mock_anthropic}):
            provider = ClaudeDirectProvider(api_key="test-key")
            result = provider.health_check()

            assert result is True
            mock_client.messages.create.assert_called_once()

    def test_health_check_failure(self):
        """Test health_check returns False when API call fails."""
        mock_anthropic = MagicMock()
        mock_client = MagicMock()
        mock_anthropic.Anthropic.return_value = mock_client
        mock_client.messages.create.side_effect = Exception("API Error")

        with patch.dict("sys.modules", {"anthropic": mock_anthropic}):
            provider = ClaudeDirectProvider(api_key="test-key")
            result = provider.health_check()

            assert result is False

    def test_summarize_success(self):
        """Test summarize returns expected summary."""
        mock_anthropic = MagicMock()
        mock_client = MagicMock()
        mock_anthropic.Anthropic.return_value = mock_client
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="This is a summary.")]
        mock_client.messages.create.return_value = mock_response

        with patch.dict("sys.modules", {"anthropic": mock_anthropic}):
            provider = ClaudeDirectProvider(api_key="test-key")
            result = provider.summarize(["Hello", "World"])

            assert result == "This is a summary."
            mock_client.messages.create.assert_called_once()

    def test_summarize_empty_messages(self):
        """Test summarize returns empty string for empty messages."""
        provider = ClaudeDirectProvider(api_key="test-key")
        result = provider.summarize([])

        assert result == ""

    def test_get_client_raises_import_error(self):
        """Test _get_client raises ImportError when anthropic not installed."""
        provider = ClaudeDirectProvider(api_key="test-key")
        with patch.dict("sys.modules", {"anthropic": None}):
            # Force reimport to trigger ImportError
            provider._client = None
            with pytest.raises(ImportError, match="anthropic package is required"):
                provider._get_client()

    def test_summarize_structured_success(self):
        """Test summarize_structured returns DecisionSummary with valid JSON response."""
        mock_anthropic = MagicMock()
        mock_client = MagicMock()
        mock_anthropic.Anthropic.return_value = mock_client

        # Mock a valid JSON response
        json_response = json.dumps({
            "type": "decision_summary",
            "topic": "API refactoring",
            "problem_statement": "Current API is slow",
            "proposed_solution": "Add caching layer",
            "requires_code_change": True,
            "affected_components": ["api.py", "cache.py"],
            "risk_level": "medium",
            "next_steps": ["Review PR", "Run tests"]
        })
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text=json_response)]
        mock_client.messages.create.return_value = mock_response

        with patch.dict("sys.modules", {"anthropic": mock_anthropic}):
            provider = ClaudeDirectProvider(api_key="test-key")
            messages = [
                ChatMessage(role="host", text="The API is slow", timestamp=1234567890),
                ChatMessage(role="engineer", text="Let's add caching", timestamp=1234567891),
            ]
            result = provider.summarize_structured(messages)

            assert isinstance(result, DecisionSummary)
            assert result.type == "decision_summary"
            assert result.topic == "API refactoring"
            assert result.problem_statement == "Current API is slow"
            assert result.proposed_solution == "Add caching layer"
            assert result.requires_code_change is True
            assert result.affected_components == ["api.py", "cache.py"]
            assert result.risk_level == "medium"
            assert result.next_steps == ["Review PR", "Run tests"]

    def test_summarize_structured_empty_messages(self):
        """Test summarize_structured returns empty DecisionSummary for empty messages."""
        provider = ClaudeDirectProvider(api_key="test-key")
        result = provider.summarize_structured([])

        assert isinstance(result, DecisionSummary)
        assert result.type == "decision_summary"
        assert result.topic == ""
        assert result.requires_code_change is False

    def test_summarize_structured_invalid_json(self):
        """Test summarize_structured raises ValueError for invalid JSON response."""
        mock_anthropic = MagicMock()
        mock_client = MagicMock()
        mock_anthropic.Anthropic.return_value = mock_client

        # Mock an invalid JSON response
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="This is not JSON")]
        mock_client.messages.create.return_value = mock_response

        with patch.dict("sys.modules", {"anthropic": mock_anthropic}):
            provider = ClaudeDirectProvider(api_key="test-key")
            messages = [ChatMessage(role="host", text="Hello", timestamp=1234567890)]

            with pytest.raises(ValueError, match="Invalid JSON response"):
                provider.summarize_structured(messages)


class TestClaudeBedrockProvider:
    """Tests for ClaudeBedrockProvider implementation."""

    def test_initialization_with_defaults(self):
        """Test provider initialization with default values."""
        provider = ClaudeBedrockProvider()
        assert provider.aws_access_key_id is None
        assert provider.aws_secret_access_key is None
        assert provider.region_name == ClaudeBedrockProvider.DEFAULT_REGION
        assert provider.model_id == ClaudeBedrockProvider.DEFAULT_MODEL_ID

    def test_initialization_with_custom_values(self):
        """Test provider initialization with custom values."""
        provider = ClaudeBedrockProvider(
            aws_access_key_id="AKIATEST",
            aws_secret_access_key="secret",
            aws_session_token="token",
            region_name="eu-west-1",
            model_id="anthropic.claude-3-opus-20240229-v1:0"
        )
        assert provider.aws_access_key_id == "AKIATEST"
        assert provider.aws_secret_access_key == "secret"
        assert provider.aws_session_token == "token"
        assert provider.region_name == "eu-west-1"
        assert provider.model_id == "anthropic.claude-3-opus-20240229-v1:0"

    def test_implements_ai_provider_interface(self):
        """ClaudeBedrockProvider should implement AIProvider interface."""
        provider = ClaudeBedrockProvider()
        assert isinstance(provider, AIProvider)

    def test_health_check_success(self):
        """Test health_check returns True when Bedrock is accessible."""
        mock_boto3 = MagicMock()
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client
        mock_client.converse.return_value = {
            "output": {"message": {"content": [{"text": "ok"}]}}
        }

        with patch.dict("sys.modules", {"boto3": mock_boto3}):
            provider = ClaudeBedrockProvider()
            result = provider.health_check()

            assert result is True
            mock_client.converse.assert_called_once()

    def test_health_check_failure(self):
        """Test health_check returns False when Bedrock call fails."""
        mock_boto3 = MagicMock()
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client
        mock_client.converse.side_effect = Exception("Bedrock Error")

        with patch.dict("sys.modules", {"boto3": mock_boto3}):
            provider = ClaudeBedrockProvider()
            result = provider.health_check()

            assert result is False

    def test_summarize_success(self):
        """Test summarize returns expected summary from Bedrock."""
        mock_boto3 = MagicMock()
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client
        mock_client.converse.return_value = {
            "output": {"message": {"content": [{"text": "Bedrock summary result."}]}}
        }

        with patch.dict("sys.modules", {"boto3": mock_boto3}):
            provider = ClaudeBedrockProvider()
            result = provider.summarize(["Message 1", "Message 2"])

            assert result == "Bedrock summary result."
            mock_client.converse.assert_called_once()

    def test_summarize_empty_messages(self):
        """Test summarize returns empty string for empty messages."""
        provider = ClaudeBedrockProvider()
        result = provider.summarize([])

        assert result == ""

    def test_get_client_raises_import_error(self):
        """Test _get_client raises ImportError when boto3 not installed."""
        provider = ClaudeBedrockProvider()
        with patch.dict("sys.modules", {"boto3": None}):
            provider._client = None
            with pytest.raises(ImportError, match="boto3 package is required"):
                provider._get_client()

    def test_client_uses_credentials_when_provided(self):
        """Test that AWS credentials are passed to boto3 client."""
        mock_boto3 = MagicMock()
        with patch.dict("sys.modules", {"boto3": mock_boto3}):
            provider = ClaudeBedrockProvider(
                aws_access_key_id="AKIATEST",
                aws_secret_access_key="secret123",
                aws_session_token="token456",
                region_name="us-west-2"
            )
            provider._get_client()

            mock_boto3.client.assert_called_once_with(
                "bedrock-runtime",
                region_name="us-west-2",
                aws_access_key_id="AKIATEST",
                aws_secret_access_key="secret123",
                aws_session_token="token456"
            )

    def test_summarize_structured_success(self):
        """Test summarize_structured returns DecisionSummary with valid JSON response."""
        mock_boto3 = MagicMock()
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client

        # Mock a valid JSON response via Converse API
        json_response = json.dumps({
            "type": "decision_summary",
            "topic": "Database migration",
            "problem_statement": "Need to migrate to PostgreSQL",
            "proposed_solution": "Use Alembic for migrations",
            "requires_code_change": True,
            "affected_components": ["models.py", "migrations/"],
            "risk_level": "high",
            "next_steps": ["Backup data", "Test migration"]
        })
        mock_client.converse.return_value = {
            "output": {"message": {"content": [{"text": json_response}]}}
        }

        with patch.dict("sys.modules", {"boto3": mock_boto3}):
            provider = ClaudeBedrockProvider()
            messages = [
                ChatMessage(role="host", text="We need to migrate DB", timestamp=1234567890),
                ChatMessage(role="engineer", text="Let's use Alembic", timestamp=1234567891),
            ]
            result = provider.summarize_structured(messages)

            assert isinstance(result, DecisionSummary)
            assert result.type == "decision_summary"
            assert result.topic == "Database migration"
            assert result.problem_statement == "Need to migrate to PostgreSQL"
            assert result.proposed_solution == "Use Alembic for migrations"
            assert result.requires_code_change is True
            assert result.affected_components == ["models.py", "migrations/"]
            assert result.risk_level == "high"
            assert result.next_steps == ["Backup data", "Test migration"]

    def test_summarize_structured_empty_messages(self):
        """Test summarize_structured returns empty DecisionSummary for empty messages."""
        provider = ClaudeBedrockProvider()
        result = provider.summarize_structured([])

        assert isinstance(result, DecisionSummary)
        assert result.type == "decision_summary"
        assert result.topic == ""
        assert result.requires_code_change is False

    def test_summarize_structured_invalid_json(self):
        """Test summarize_structured raises ValueError for invalid JSON response."""
        mock_boto3 = MagicMock()
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client

        # Mock an invalid JSON response via Converse API
        mock_client.converse.return_value = {
            "output": {"message": {"content": [{"text": "Not valid JSON at all"}]}}
        }

        with patch.dict("sys.modules", {"boto3": mock_boto3}):
            provider = ClaudeBedrockProvider()
            messages = [ChatMessage(role="host", text="Hello", timestamp=1234567890)]

            with pytest.raises(ValueError, match="Invalid JSON response"):
                provider.summarize_structured(messages)


class TestAllProvidersImplementSameInterface:
    """Tests to verify all providers implement the same interface."""

    def test_all_have_health_check_method(self):
        """All providers should have health_check method."""
        direct = ClaudeDirectProvider(api_key="test")
        bedrock = ClaudeBedrockProvider()

        assert callable(getattr(direct, "health_check", None))
        assert callable(getattr(bedrock, "health_check", None))

    def test_all_have_summarize_method(self):
        """All providers should have summarize method."""
        direct = ClaudeDirectProvider(api_key="test")
        bedrock = ClaudeBedrockProvider()

        assert callable(getattr(direct, "summarize", None))
        assert callable(getattr(bedrock, "summarize", None))

    def test_all_have_summarize_structured_method(self):
        """All providers should have summarize_structured method."""
        direct = ClaudeDirectProvider(api_key="test")
        bedrock = ClaudeBedrockProvider()

        assert callable(getattr(direct, "summarize_structured", None))
        assert callable(getattr(bedrock, "summarize_structured", None))

    def test_all_are_instances_of_ai_provider(self):
        """All providers should be instances of AIProvider."""
        direct = ClaudeDirectProvider(api_key="test")
        bedrock = ClaudeBedrockProvider()

        assert isinstance(direct, AIProvider)
        assert isinstance(bedrock, AIProvider)


def _make_conductor_config(
    enabled=True,
    bedrock_access_key="",
    bedrock_secret_key="",
    bedrock_session_token="",
    anthropic_api_key="",
    openai_api_key="",
    default_model="claude-3-haiku-bedrock",
):
    """Helper to build a ConductorConfig for resolver tests."""
    from app.config import (
        ConductorConfig, SummaryConfig, AIProviderSettingsConfig,
        AIProvidersSecretsConfig, AWSBedrockSecretsConfig,
        AnthropicSecretsConfig, OpenAISecretsConfig, AIModelConfig,
    )
    return ConductorConfig(
        summary=SummaryConfig(enabled=enabled, default_model=default_model),
        ai_provider_settings=AIProviderSettingsConfig(
            anthropic_enabled=bool(anthropic_api_key),
            aws_bedrock_enabled=bool(bedrock_access_key),
            openai_enabled=bool(openai_api_key),
        ),
        ai_providers=AIProvidersSecretsConfig(
            anthropic=AnthropicSecretsConfig(api_key=anthropic_api_key),
            aws_bedrock=AWSBedrockSecretsConfig(
                access_key_id=bedrock_access_key,
                secret_access_key=bedrock_secret_key,
                session_token=bedrock_session_token,
            ),
            openai=OpenAISecretsConfig(api_key=openai_api_key),
        ),
        ai_models=[
            AIModelConfig(
                id="claude-3-haiku-bedrock",
                provider="aws_bedrock",
                model_name="anthropic.claude-3-haiku-20240307-v1:0",
                display_name="Claude 3 Haiku (Bedrock)",
            ),
            AIModelConfig(
                id="claude-sonnet-4-anthropic",
                provider="anthropic",
                model_name="claude-sonnet-4-20250514",
                display_name="Claude Sonnet 4 (Anthropic)",
            ),
        ],
    )


class TestProviderResolver:
    """Tests for ProviderResolver service."""

    def test_resolve_disabled_returns_none(self):
        """When summary is disabled, resolve should return None."""
        from app.ai_provider.resolver import ProviderResolver

        config = _make_conductor_config(enabled=False)
        resolver = ProviderResolver(config)
        result = resolver.resolve()

        assert result is None
        assert resolver.get_active_provider() is None
        assert resolver.active_provider_type is None

    def test_resolve_skips_empty_api_keys(self):
        """Resolver should skip providers with empty API keys."""
        from app.ai_provider.resolver import ProviderResolver

        config = _make_conductor_config(
            enabled=True,
            bedrock_access_key="",
            anthropic_api_key="",
        )
        resolver = ProviderResolver(config)
        result = resolver.resolve()

        assert result is None
        assert resolver.get_active_provider() is None

    def test_resolve_first_healthy_provider_bedrock(self):
        """Resolver should select first healthy provider (bedrock has priority)."""
        from app.ai_provider.resolver import ProviderResolver

        config = _make_conductor_config(
            enabled=True,
            bedrock_access_key="AKIATEST",
            bedrock_secret_key="secret123",
            anthropic_api_key="sk-ant-test",
        )

        mock_boto3 = MagicMock()
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client
        mock_client.converse.return_value = {
            "output": {"message": {"content": [{"text": "ok"}]}}
        }

        with patch.dict("sys.modules", {"boto3": mock_boto3}):
            resolver = ProviderResolver(config)
            result = resolver.resolve()

            assert result is not None
            assert resolver.active_provider_type == "aws_bedrock"

    def test_resolve_falls_back_to_direct_provider(self):
        """Resolver should fall back to anthropic if bedrock is unhealthy."""
        from app.ai_provider.resolver import ProviderResolver

        config = _make_conductor_config(
            enabled=True,
            bedrock_access_key="AKIATEST",
            bedrock_secret_key="secret123",
            anthropic_api_key="sk-ant-test",
            default_model="claude-sonnet-4-anthropic",
        )

        mock_boto3 = MagicMock()
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client
        mock_client.converse.side_effect = Exception("Bedrock error")

        mock_anthropic = MagicMock()
        mock_anthropic_client = MagicMock()
        mock_anthropic.Anthropic.return_value = mock_anthropic_client
        mock_anthropic_client.messages.create.return_value = MagicMock()

        with patch.dict("sys.modules", {"boto3": mock_boto3, "anthropic": mock_anthropic}):
            resolver = ProviderResolver(config)
            result = resolver.resolve()

            assert result is not None
            assert resolver.active_provider_type == "anthropic"
            assert resolver._provider_health.get("aws_bedrock") is False
            assert resolver._provider_health.get("anthropic") is True

    def test_resolve_no_healthy_provider(self):
        """Resolver should return None when no providers are healthy."""
        from app.ai_provider.resolver import ProviderResolver

        config = _make_conductor_config(
            enabled=True,
            bedrock_access_key="AKIATEST",
            bedrock_secret_key="secret123",
        )

        mock_boto3 = MagicMock()
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client
        mock_client.converse.side_effect = Exception("Bedrock error")

        with patch.dict("sys.modules", {"boto3": mock_boto3}):
            resolver = ProviderResolver(config)
            result = resolver.resolve()

            assert result is None
            assert resolver.get_active_provider() is None

    def test_get_status_returns_correct_structure(self):
        """get_status should return AIStatus with correct data."""
        from app.ai_provider.resolver import ProviderResolver

        config = _make_conductor_config(
            enabled=True,
            bedrock_access_key="AKIATEST",
            bedrock_secret_key="secret123",
        )

        mock_boto3 = MagicMock()
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client
        mock_client.converse.return_value = {
            "output": {"message": {"content": [{"text": "ok"}]}}
        }

        with patch.dict("sys.modules", {"boto3": mock_boto3}):
            resolver = ProviderResolver(config)
            resolver.resolve()
            status = resolver.get_status()

            assert status.summary_enabled is True
            assert status.active_provider == "aws_bedrock"
            # All 3 provider types are listed
            assert len(status.providers) == 3
            bedrock_status = [p for p in status.providers if p.name == "aws_bedrock"][0]
            assert bedrock_status.healthy is True

    def test_bedrock_api_key_with_session_token(self):
        """Resolver should use session token from bedrock config."""
        from app.ai_provider.resolver import ProviderResolver

        config = _make_conductor_config(
            enabled=True,
            bedrock_access_key="AKIATEST",
            bedrock_secret_key="secret123",
            bedrock_session_token="session456",
        )

        mock_boto3 = MagicMock()
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client
        mock_client.converse.return_value = {
            "output": {"message": {"content": [{"text": "ok"}]}}
        }

        with patch.dict("sys.modules", {"boto3": mock_boto3}):
            resolver = ProviderResolver(config)
            resolver.resolve()

            # Verify boto3 was called with session token
            mock_boto3.client.assert_called_once()
            call_kwargs = mock_boto3.client.call_args[1]
            assert call_kwargs["aws_access_key_id"] == "AKIATEST"
            assert call_kwargs["aws_secret_access_key"] == "secret123"
            assert call_kwargs["aws_session_token"] == "session456"

    def test_no_configured_provider(self):
        """Resolver should return None when no provider is configured."""
        from app.ai_provider.resolver import ProviderResolver

        config = _make_conductor_config(enabled=True)  # all keys empty
        resolver = ProviderResolver(config)
        result = resolver.resolve()

        assert result is None
        assert resolver._provider_health.get("aws_bedrock") is False


class TestAIStatusEndpoint:
    """Tests for GET /ai/status endpoint."""

    def test_status_when_resolver_not_initialized(self):
        """Endpoint should return disabled status when resolver not set."""
        from fastapi.testclient import TestClient
        from app.ai_provider.resolver import set_resolver
        from app.ai_provider.router import router
        from fastapi import FastAPI

        # Create a test app with just the AI router
        test_app = FastAPI()
        test_app.include_router(router)

        # Ensure resolver is not set
        set_resolver(None)

        client = TestClient(test_app)
        response = client.get("/ai/status")

        assert response.status_code == 200
        data = response.json()
        assert data["summary_enabled"] is False
        assert data["active_provider"] is None
        assert data["providers"] == []

    def test_status_with_active_provider(self):
        """Endpoint should return correct status with active provider."""
        from fastapi.testclient import TestClient
        from app.ai_provider.resolver import ProviderResolver, set_resolver
        from app.ai_provider.router import router
        from fastapi import FastAPI

        test_app = FastAPI()
        test_app.include_router(router)

        # Set up resolver with mocked healthy bedrock provider
        config = _make_conductor_config(
            enabled=True,
            bedrock_access_key="AKIATEST",
            bedrock_secret_key="secret123",
        )

        mock_boto3 = MagicMock()
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client
        mock_client.converse.return_value = {"output": {"message": {"content": [{"text": "ok"}]}}}

        with patch.dict("sys.modules", {"boto3": mock_boto3}):
            resolver = ProviderResolver(config)
            resolver.resolve()
            set_resolver(resolver)

            client = TestClient(test_app)
            response = client.get("/ai/status")

            assert response.status_code == 200
            data = response.json()
            assert data["summary_enabled"] is True
            assert data["active_provider"] == "aws_bedrock"
            assert len(data["providers"]) == 3
            bedrock_provider = [p for p in data["providers"] if p["name"] == "aws_bedrock"][0]
            assert bedrock_provider["healthy"] is True

        # Clean up
        set_resolver(None)

    def test_status_with_no_healthy_provider(self):
        """Endpoint should show null active_provider when none healthy."""
        from fastapi.testclient import TestClient
        from app.ai_provider.resolver import ProviderResolver, set_resolver
        from app.ai_provider.router import router
        from fastapi import FastAPI

        test_app = FastAPI()
        test_app.include_router(router)

        config = _make_conductor_config(
            enabled=True,
            bedrock_access_key="AKIATEST",
            bedrock_secret_key="secret123",
        )

        mock_boto3 = MagicMock()
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client
        mock_client.converse.side_effect = Exception("Bedrock error")

        with patch.dict("sys.modules", {"boto3": mock_boto3}):
            resolver = ProviderResolver(config)
            resolver.resolve()
            set_resolver(resolver)

            client = TestClient(test_app)
            response = client.get("/ai/status")

            assert response.status_code == 200
            data = response.json()
            assert data["summary_enabled"] is True
            assert data["active_provider"] is None
            assert len(data["providers"]) == 3
            bedrock_provider = [p for p in data["providers"] if p["name"] == "aws_bedrock"][0]
            assert bedrock_provider["healthy"] is False

        # Clean up
        set_resolver(None)


class TestSummarizeEndpoint:
    """Tests for POST /ai/summarize endpoint."""

    def test_summarize_returns_503_when_resolver_not_initialized(self):
        """Endpoint should return 503 when resolver is not set."""
        from fastapi.testclient import TestClient
        from app.ai_provider.resolver import set_resolver
        from app.ai_provider.router import router
        from fastapi import FastAPI

        test_app = FastAPI()
        test_app.include_router(router)

        # Ensure resolver is not set
        set_resolver(None)

        client = TestClient(test_app)
        response = client.post("/ai/summarize", json={
            "messages": [{"role": "host", "text": "Hello", "timestamp": 1234567890}]
        })

        assert response.status_code == 503
        assert "not initialized" in response.json()["detail"]

    def test_summarize_returns_503_when_summary_disabled(self):
        """Endpoint should return 503 when summary is disabled."""
        from fastapi.testclient import TestClient
        from app.ai_provider.resolver import ProviderResolver, set_resolver
        from app.ai_provider.router import router
        from fastapi import FastAPI

        test_app = FastAPI()
        test_app.include_router(router)

        # Set up resolver with summary disabled
        config = _make_conductor_config(enabled=False)
        resolver = ProviderResolver(config)
        set_resolver(resolver)

        client = TestClient(test_app)
        response = client.post("/ai/summarize", json={
            "messages": [{"role": "host", "text": "Hello", "timestamp": 1234567890}]
        })

        assert response.status_code == 503
        assert "not enabled" in response.json()["detail"]

        # Clean up
        set_resolver(None)

    def test_summarize_returns_503_when_no_active_provider(self):
        """Endpoint should return 503 when no active provider available."""
        from fastapi.testclient import TestClient
        from app.ai_provider.resolver import ProviderResolver, set_resolver
        from app.ai_provider.router import router
        from fastapi import FastAPI

        test_app = FastAPI()
        test_app.include_router(router)

        config = _make_conductor_config(
            enabled=True,
            bedrock_access_key="AKIATEST",
            bedrock_secret_key="secret123",
        )

        # All providers fail health check
        mock_boto3 = MagicMock()
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client
        mock_client.converse.side_effect = Exception("Bedrock error")

        with patch.dict("sys.modules", {"boto3": mock_boto3}):
            resolver = ProviderResolver(config)
            resolver.resolve()
            set_resolver(resolver)

            client = TestClient(test_app)
            response = client.post("/ai/summarize", json={
                "messages": [{"role": "host", "text": "Hello", "timestamp": 1234567890}]
            })

            assert response.status_code == 503
            assert "No active AI provider" in response.json()["detail"]

        # Clean up
        set_resolver(None)

    def test_summarize_success(self):
        """Endpoint should return structured summary when provider is available."""
        from fastapi.testclient import TestClient
        from app.ai_provider.resolver import ProviderResolver, set_resolver
        from app.ai_provider.router import router
        from fastapi import FastAPI

        test_app = FastAPI()
        test_app.include_router(router)

        config = _make_conductor_config(
            enabled=True,
            anthropic_api_key="sk-ant-test",
            default_model="claude-sonnet-4-anthropic",
        )

        # Mock anthropic module for health check
        mock_anthropic = MagicMock()
        mock_anthropic_client = MagicMock()
        mock_anthropic.Anthropic.return_value = mock_anthropic_client
        mock_anthropic_client.messages.create.return_value = MagicMock()

        with patch.dict("sys.modules", {"anthropic": mock_anthropic}):
            resolver = ProviderResolver(config)
            resolver.resolve()
            set_resolver(resolver)

            # Mock call_model for the pipeline (classification + summary + items)
            with patch.object(
                resolver.get_active_provider(),
                "call_model",
                side_effect=[
                    # First call: classification
                    json.dumps({"discussion_type": "general", "confidence": 0.8}),
                    # Second call: targeted summary
                    json.dumps({
                        "type": "decision_summary",
                        "topic": "Test topic",
                        "core_problem": "Test problem",
                        "proposed_solution": "Test solution",
                        "requires_code_change": True,
                        "impact_scope": "module",
                        "affected_components": ["file1.py", "file2.py"],
                        "risk_level": "medium",
                        "next_steps": ["Step 1", "Step 2"]
                    }),
                    # Third call: item extraction (stage 4)
                    json.dumps([{
                        "id": "item-1",
                        "type": "code_change",
                        "title": "Test item",
                        "problem": "Test problem",
                        "proposed_change": "Test change",
                        "targets": ["file1.py"],
                        "risk_level": "medium",
                    }])
                ]
            ):
                client = TestClient(test_app)
                response = client.post(
                    "/ai/summarize",
                    json={
                        "messages": [
                            {"role": "host", "text": "Hello", "timestamp": 1234567890},
                            {"role": "engineer", "text": "World", "timestamp": 1234567891}
                        ]
                    }
                )

                assert response.status_code == 200
                data = response.json()
                # Verify all required JSON keys exist
                assert data["type"] == "decision_summary"
                assert data["topic"] == "Test topic"
                assert data["problem_statement"] == "Test problem"  # Mapped from core_problem
                assert data["proposed_solution"] == "Test solution"
                assert data["requires_code_change"] is True
                assert data["affected_components"] == ["file1.py", "file2.py"]
                assert data["risk_level"] == "medium"
                assert data["next_steps"] == ["Step 1", "Step 2"]
                # Verify pipeline metadata
                assert data["discussion_type"] == "general"
                assert data["classification_confidence"] == 0.8
                # Verify code_relevant_items
                assert len(data["code_relevant_items"]) == 1
                assert data["code_relevant_items"][0]["title"] == "Test item"

        # Clean up
        set_resolver(None)

    def test_summarize_returns_500_on_provider_error(self):
        """Endpoint should return 500 when provider raises an error."""
        from fastapi.testclient import TestClient
        from app.ai_provider.resolver import ProviderResolver, set_resolver
        from app.ai_provider.router import router
        from fastapi import FastAPI

        test_app = FastAPI()
        test_app.include_router(router)

        config = _make_conductor_config(
            enabled=True,
            anthropic_api_key="sk-ant-test",
            default_model="claude-sonnet-4-anthropic",
        )

        # Mock anthropic module for health check
        mock_anthropic = MagicMock()
        mock_anthropic_client = MagicMock()
        mock_anthropic.Anthropic.return_value = mock_anthropic_client
        mock_anthropic_client.messages.create.return_value = MagicMock()

        with patch.dict("sys.modules", {"anthropic": mock_anthropic}):
            resolver = ProviderResolver(config)
            resolver.resolve()
            set_resolver(resolver)

        # Mock summarize_structured to raise an error
        with patch.object(
            resolver.get_active_provider(),
            "summarize_structured",
            side_effect=RuntimeError("Provider error")
        ):
            client = TestClient(test_app)
            response = client.post(
                "/ai/summarize",
                json={
                    "messages": [{"role": "host", "text": "Hello", "timestamp": 1234567890}]
                }
            )

            assert response.status_code == 500
            detail = response.json()["detail"]
            # The wrapper format includes "Provider <name> error: <message>"
            assert "Provider" in detail or "error" in detail.lower()

        # Clean up
        set_resolver(None)

    def test_summarize_empty_messages(self):
        """Endpoint should handle empty messages list with default DecisionSummary."""
        from fastapi.testclient import TestClient
        from app.ai_provider.resolver import ProviderResolver, set_resolver
        from app.ai_provider.router import router
        from fastapi import FastAPI

        test_app = FastAPI()
        test_app.include_router(router)

        config = _make_conductor_config(
            enabled=True,
            anthropic_api_key="sk-ant-test",
            default_model="claude-sonnet-4-anthropic",
        )

        # Mock anthropic module for health check
        mock_anthropic = MagicMock()
        mock_anthropic_client = MagicMock()
        mock_anthropic.Anthropic.return_value = mock_anthropic_client
        mock_anthropic_client.messages.create.return_value = MagicMock()

        with patch.dict("sys.modules", {"anthropic": mock_anthropic}):
            resolver = ProviderResolver(config)
            resolver.resolve()
            set_resolver(resolver)

            client = TestClient(test_app)
            response = client.post("/ai/summarize", json={"messages": []})

            assert response.status_code == 200
            data = response.json()
            # Empty messages should return default DecisionSummary
            assert data["type"] == "decision_summary"
            assert data["topic"] == ""
            assert data["requires_code_change"] is False

        # Clean up
        set_resolver(None)

    def test_summarize_returns_500_on_json_parsing_error(self):
        """Endpoint should return 500 with retry suggestion when JSON parsing fails."""
        from fastapi.testclient import TestClient
        from app.ai_provider.resolver import ProviderResolver, set_resolver
        from app.ai_provider.router import router
        from fastapi import FastAPI

        test_app = FastAPI()
        test_app.include_router(router)

        config = _make_conductor_config(
            enabled=True,
            anthropic_api_key="sk-ant-test",
            default_model="claude-sonnet-4-anthropic",
        )

        # Mock anthropic module for health check
        mock_anthropic = MagicMock()
        mock_anthropic_client = MagicMock()
        mock_anthropic.Anthropic.return_value = mock_anthropic_client
        mock_anthropic_client.messages.create.return_value = MagicMock()

        with patch.dict("sys.modules", {"anthropic": mock_anthropic}):
            resolver = ProviderResolver(config)
            resolver.resolve()
            set_resolver(resolver)

        # Mock summarize_structured to raise ValueError (JSON parsing error)
        with patch.object(
            resolver.get_active_provider(),
            "summarize_structured",
            side_effect=ValueError("Invalid JSON response from AI: Expecting value")
        ):
            client = TestClient(test_app)
            response = client.post(
                "/ai/summarize",
                json={
                    "messages": [{"role": "host", "text": "Hello", "timestamp": 1234567890}]
                }
            )

            assert response.status_code == 500
            detail = response.json()["detail"]
            # Should mention JSON parsing failure
            assert "JSON" in detail
            # The wrapper format includes provider name and error details
            assert "anthropic" in detail or "parse" in detail.lower()

        # Clean up
        set_resolver(None)


class TestCodePromptEndpoint:
    """Tests for POST /ai/code-prompt endpoint."""

    def test_code_prompt_success(self):
        """Endpoint should return a code prompt from decision summary."""
        from fastapi.testclient import TestClient
        from app.ai_provider.router import router
        from fastapi import FastAPI

        test_app = FastAPI()
        test_app.include_router(router)

        client = TestClient(test_app)
        response = client.post("/ai/code-prompt", json={
            "decision_summary": {
                "type": "decision_summary",
                "topic": "Add user authentication",
                "problem_statement": "Users cannot log in securely",
                "proposed_solution": "Implement JWT-based authentication",
                "requires_code_change": True,
                "affected_components": ["auth/login.py", "auth/middleware.py"],
                "risk_level": "medium",
                "next_steps": ["Implement login endpoint", "Add JWT validation"]
            }
        })

        assert response.status_code == 200
        data = response.json()
        assert "code_prompt" in data
        assert "Users cannot log in securely" in data["code_prompt"]
        assert "JWT-based authentication" in data["code_prompt"]
        assert "auth/login.py" in data["code_prompt"]
        assert "auth/middleware.py" in data["code_prompt"]
        assert "medium" in data["code_prompt"]

    def test_code_prompt_with_context_snippet(self):
        """Endpoint should include context snippet in the prompt."""
        from fastapi.testclient import TestClient
        from app.ai_provider.router import router
        from fastapi import FastAPI

        test_app = FastAPI()
        test_app.include_router(router)

        client = TestClient(test_app)
        response = client.post("/ai/code-prompt", json={
            "decision_summary": {
                "type": "decision_summary",
                "topic": "Fix login bug",
                "problem_statement": "Login fails silently",
                "proposed_solution": "Add error handling",
                "requires_code_change": True,
                "affected_components": ["auth/login.py"],
                "risk_level": "low",
                "next_steps": ["Add try-catch"]
            },
            "context_snippet": "def login(username, password):\n    pass"
        })

        assert response.status_code == 200
        data = response.json()
        assert "code_prompt" in data
        assert "def login(username, password)" in data["code_prompt"]
        assert "<context>" in data["code_prompt"]

    def test_code_prompt_empty_components(self):
        """Endpoint should handle empty affected_components list."""
        from fastapi.testclient import TestClient
        from app.ai_provider.router import router
        from fastapi import FastAPI

        test_app = FastAPI()
        test_app.include_router(router)

        client = TestClient(test_app)
        response = client.post("/ai/code-prompt", json={
            "decision_summary": {
                "type": "decision_summary",
                "topic": "General improvement",
                "problem_statement": "Code needs refactoring",
                "proposed_solution": "Refactor the module",
                "requires_code_change": True,
                "affected_components": [],
                "risk_level": "low",
                "next_steps": []
            }
        })

        assert response.status_code == 200
        data = response.json()
        assert "code_prompt" in data
        assert "No specific components identified" in data["code_prompt"]

    def test_code_prompt_invalid_request_missing_summary(self):
        """Endpoint should return 422 for missing decision_summary."""
        from fastapi.testclient import TestClient
        from app.ai_provider.router import router
        from fastapi import FastAPI

        test_app = FastAPI()
        test_app.include_router(router)

        client = TestClient(test_app)
        response = client.post("/ai/code-prompt", json={})

        assert response.status_code == 422

    def test_code_prompt_invalid_risk_level(self):
        """Endpoint should return 422 for invalid risk_level."""
        from fastapi.testclient import TestClient
        from app.ai_provider.router import router
        from fastapi import FastAPI

        test_app = FastAPI()
        test_app.include_router(router)

        client = TestClient(test_app)
        response = client.post("/ai/code-prompt", json={
            "decision_summary": {
                "type": "decision_summary",
                "topic": "Test",
                "problem_statement": "Test problem",
                "proposed_solution": "Test solution",
                "requires_code_change": True,
                "affected_components": [],
                "risk_level": "invalid_level",  # Invalid value
                "next_steps": []
            }
        })

        assert response.status_code == 422


class TestGetCodePromptFunction:
    """Tests for the get_code_prompt function in prompts.py."""

    def test_get_code_prompt_basic(self):
        """Function should generate a code prompt with all fields."""
        from app.ai_provider.prompts import get_code_prompt

        prompt = get_code_prompt(
            problem_statement="Users cannot log in",
            proposed_solution="Add authentication",
            affected_components=["auth.py", "login.py"],
            risk_level="medium"
        )

        assert "Users cannot log in" in prompt
        assert "Add authentication" in prompt
        assert "auth.py" in prompt
        assert "login.py" in prompt
        assert "medium" in prompt
        assert "unified diff" in prompt.lower()

    def test_get_code_prompt_with_context(self):
        """Function should include context snippet when provided."""
        from app.ai_provider.prompts import get_code_prompt

        prompt = get_code_prompt(
            problem_statement="Bug in login",
            proposed_solution="Fix the bug",
            affected_components=["login.py"],
            risk_level="low",
            context_snippet="def login():\n    return None"
        )

        assert "def login():" in prompt
        assert "<context>" in prompt

    def test_get_code_prompt_empty_components(self):
        """Function should handle empty components list."""
        from app.ai_provider.prompts import get_code_prompt

        prompt = get_code_prompt(
            problem_statement="Test",
            proposed_solution="Test",
            affected_components=[],
            risk_level="low"
        )

        assert "No specific components identified" in prompt

    def test_get_code_prompt_none_values(self):
        """Function should handle None values gracefully."""
        from app.ai_provider.prompts import get_code_prompt

        prompt = get_code_prompt(
            problem_statement=None,
            proposed_solution=None,
            affected_components=None,
            risk_level=None
        )

        assert "No problem statement provided" in prompt
        assert "No solution proposed" in prompt
        assert "No specific components identified" in prompt
        assert "unknown" in prompt


class TestAIProviderWrapper:
    """Tests for the AI provider wrapper functions."""

    def test_call_summary_raises_when_resolver_not_initialized(self):
        """call_summary should raise ProviderNotAvailableError when resolver is None."""
        from app.ai_provider.wrapper import call_summary, ProviderNotAvailableError
        from app.ai_provider.resolver import set_resolver

        # Ensure resolver is not set
        set_resolver(None)

        with pytest.raises(ProviderNotAvailableError) as exc_info:
            call_summary([])

        assert "not initialized" in str(exc_info.value.message)
        assert exc_info.value.status_code == 503

    def test_call_summary_raises_when_disabled(self):
        """call_summary should raise ProviderNotAvailableError when summary is disabled."""
        from app.ai_provider.resolver import ProviderResolver, set_resolver
        from app.ai_provider.wrapper import call_summary, ProviderNotAvailableError

        config = _make_conductor_config(enabled=False)
        resolver = ProviderResolver(config)
        set_resolver(resolver)

        with pytest.raises(ProviderNotAvailableError) as exc_info:
            call_summary([])

        assert "not enabled" in str(exc_info.value.message)
        assert exc_info.value.status_code == 503

        # Cleanup
        set_resolver(None)

    def test_call_summary_raises_when_no_provider_available(self):
        """call_summary should raise ProviderNotAvailableError when no provider is configured."""
        from app.ai_provider.resolver import ProviderResolver, set_resolver
        from app.ai_provider.wrapper import call_summary, ProviderNotAvailableError

        config = _make_conductor_config(enabled=True)
        resolver = ProviderResolver(config)
        resolver.resolve()  # Will find no providers
        set_resolver(resolver)

        with pytest.raises(ProviderNotAvailableError) as exc_info:
            call_summary([])

        assert "No active AI provider" in str(exc_info.value.message)
        assert exc_info.value.status_code == 503

        # Cleanup
        set_resolver(None)

    def test_call_summary_success_with_mock_provider(self):
        """call_summary should return DecisionSummary on success."""
        from app.ai_provider.resolver import ProviderResolver, set_resolver
        from app.ai_provider.wrapper import call_summary
        from app.ai_provider import ChatMessage, DecisionSummary

        config = _make_conductor_config(
            enabled=True,
            anthropic_api_key="test-key",
            default_model="claude-sonnet-4-anthropic",
        )

        mock_anthropic = MagicMock()
        mock_client = MagicMock()
        mock_anthropic.Anthropic.return_value = mock_client
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text='{"topic": "test", "problem_statement": "prob", "proposed_solution": "sol", "requires_code_change": false, "affected_components": [], "risk_level": "low", "next_steps": []}')]
        mock_client.messages.create.return_value = mock_response

        with patch.dict("sys.modules", {"anthropic": mock_anthropic}):
            resolver = ProviderResolver(config)
            resolver.resolve()
            set_resolver(resolver)

            messages = [ChatMessage(role="host", text="Hello", timestamp=1234567890)]
            result = call_summary(messages)

            assert isinstance(result, DecisionSummary)
            assert result.topic == "test"

        # Cleanup
        set_resolver(None)

    def test_call_summary_raises_json_parse_error(self):
        """call_summary should raise JSONParseError when response is invalid JSON."""
        from app.ai_provider.resolver import ProviderResolver, set_resolver
        from app.ai_provider.wrapper import call_summary, JSONParseError
        from app.ai_provider import ChatMessage

        config = _make_conductor_config(
            enabled=True,
            anthropic_api_key="test-key",
            default_model="claude-sonnet-4-anthropic",
        )

        mock_anthropic = MagicMock()
        mock_client = MagicMock()
        mock_anthropic.Anthropic.return_value = mock_client
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text='not valid json')]
        mock_client.messages.create.return_value = mock_response

        with patch.dict("sys.modules", {"anthropic": mock_anthropic}):
            resolver = ProviderResolver(config)
            resolver.resolve()
            set_resolver(resolver)

            messages = [ChatMessage(role="host", text="Hello", timestamp=1234567890)]

            with pytest.raises(JSONParseError) as exc_info:
                call_summary(messages)

            assert exc_info.value.status_code == 500
            assert "anthropic" in exc_info.value.provider_name

        # Cleanup
        set_resolver(None)

    def test_call_summary_raises_provider_call_error_on_exception(self):
        """call_summary should raise ProviderCallError on general exceptions."""
        from app.ai_provider.resolver import ProviderResolver, set_resolver
        from app.ai_provider.wrapper import call_summary, ProviderCallError
        from app.ai_provider import ChatMessage

        config = _make_conductor_config(
            enabled=True,
            anthropic_api_key="test-key",
            default_model="claude-sonnet-4-anthropic",
        )

        # Mock successful health check response
        health_check_response = MagicMock()
        health_check_response.content = [MagicMock(text="OK")]

        mock_anthropic = MagicMock()
        mock_client = MagicMock()
        mock_anthropic.Anthropic.return_value = mock_client
        # First call succeeds (health check), second fails (summarization)
        mock_client.messages.create.side_effect = [
            health_check_response,
            Exception("API error")
        ]

        with patch.dict("sys.modules", {"anthropic": mock_anthropic}):
            resolver = ProviderResolver(config)
            resolver.resolve()
            set_resolver(resolver)

            messages = [ChatMessage(role="host", text="Hello", timestamp=1234567890)]

            with pytest.raises(ProviderCallError) as exc_info:
                call_summary(messages)

            assert exc_info.value.status_code == 500
            assert "API error" in str(exc_info.value.message)

        # Cleanup
        set_resolver(None)

    def test_call_code_prompt_returns_string(self):
        """call_code_prompt should return a formatted string."""
        from app.ai_provider.wrapper import call_code_prompt

        result = call_code_prompt(
            problem_statement="Test problem",
            proposed_solution="Test solution",
            affected_components=["file1.py", "file2.py"],
            risk_level="medium",
            context_snippet="def test(): pass"
        )

        assert isinstance(result, str)
        assert "Test problem" in result
        assert "Test solution" in result
        assert "file1.py" in result
        assert "file2.py" in result
        assert "medium" in result
        assert "def test(): pass" in result

    def test_call_code_prompt_handles_empty_components(self):
        """call_code_prompt should handle empty components list."""
        from app.ai_provider.wrapper import call_code_prompt

        result = call_code_prompt(
            problem_statement="Test problem",
            proposed_solution="Test solution",
            affected_components=[],
            risk_level="low"
        )

        assert isinstance(result, str)
        assert "Test problem" in result

    def test_handle_provider_error_converts_to_http_exception(self):
        """handle_provider_error should convert AIProviderError to HTTPException."""
        from fastapi import HTTPException
        from app.ai_provider.wrapper import (
            handle_provider_error,
            ProviderNotAvailableError,
            ProviderCallError,
            JSONParseError
        )

        # Test ProviderNotAvailableError
        error1 = ProviderNotAvailableError("No provider")
        http_exc1 = handle_provider_error(error1)
        assert isinstance(http_exc1, HTTPException)
        assert http_exc1.status_code == 503
        assert "No provider" in http_exc1.detail

        # Test ProviderCallError
        error2 = ProviderCallError("API failed", "claude_direct")
        http_exc2 = handle_provider_error(error2)
        assert isinstance(http_exc2, HTTPException)
        assert http_exc2.status_code == 500

        # Test JSONParseError
        error3 = JSONParseError("Invalid JSON", "claude_direct")
        http_exc3 = handle_provider_error(error3)
        assert isinstance(http_exc3, HTTPException)
        assert http_exc3.status_code == 500

    def test_call_summary_http_convenience_wrapper(self):
        """call_summary_http should convert exceptions to HTTPException."""
        from app.ai_provider.wrapper import call_summary_http
        from app.ai_provider.resolver import set_resolver
        from fastapi import HTTPException

        # Ensure resolver is not set
        set_resolver(None)

        with pytest.raises(HTTPException) as exc_info:
            call_summary_http([])

        assert exc_info.value.status_code == 503


class TestSummarizeEndpointWithMockProvider:
    """Extended tests for POST /ai/summarize with mock provider scenarios."""

    def test_summarize_with_multiple_message_roles(self):
        """Endpoint should handle messages from different roles (host, engineer, observer)."""
        from fastapi.testclient import TestClient
        from app.ai_provider.resolver import ProviderResolver, set_resolver
        from app.ai_provider.router import router
        from fastapi import FastAPI

        test_app = FastAPI()
        test_app.include_router(router)

        config = _make_conductor_config(enabled=True, anthropic_api_key="sk-ant-test", default_model="claude-sonnet-4-anthropic")

        mock_anthropic = MagicMock()
        mock_client = MagicMock()
        mock_anthropic.Anthropic.return_value = mock_client
        mock_client.messages.create.return_value = MagicMock()

        with patch.dict("sys.modules", {"anthropic": mock_anthropic}):
            resolver = ProviderResolver(config)
            resolver.resolve()
            set_resolver(resolver)

            # Mock call_model for the pipeline
            with patch.object(
                resolver.get_active_provider(),
                "call_model",
                side_effect=[
                    json.dumps({"discussion_type": "api_design", "confidence": 0.9}),
                    json.dumps({
                        "type": "decision_summary",
                        "topic": "Multi-role discussion",
                        "core_problem": "Complex problem",
                        "proposed_solution": "Team solution",
                        "requires_code_change": True,
                        "impact_scope": "module",
                        "affected_components": ["api.py"],
                        "risk_level": "high",
                        "next_steps": ["Review", "Implement"]
                    })
                ]
            ):
                client = TestClient(test_app)
                response = client.post("/ai/summarize", json={
                    "messages": [
                        {"role": "host", "text": "Let's discuss the API", "timestamp": 1000},
                        {"role": "engineer", "text": "I suggest REST", "timestamp": 1001},
                        {"role": "engineer", "text": "Looks good", "timestamp": 1002},
                        {"role": "host", "text": "Agreed", "timestamp": 1003}
                    ]
                })

                assert response.status_code == 200
                data = response.json()
                assert data["topic"] == "Multi-role discussion"
                assert data["risk_level"] == "high"

        set_resolver(None)

    def test_summarize_with_long_messages(self):
        """Endpoint should handle long message content."""
        from fastapi.testclient import TestClient
        from app.ai_provider.resolver import ProviderResolver, set_resolver
        from app.ai_provider.router import router
        from fastapi import FastAPI

        test_app = FastAPI()
        test_app.include_router(router)

        config = _make_conductor_config(enabled=True, anthropic_api_key="sk-ant-test", default_model="claude-sonnet-4-anthropic")

        mock_anthropic = MagicMock()
        mock_client = MagicMock()
        mock_anthropic.Anthropic.return_value = mock_client
        mock_client.messages.create.return_value = MagicMock()

        with patch.dict("sys.modules", {"anthropic": mock_anthropic}):
            resolver = ProviderResolver(config)
            resolver.resolve()
            set_resolver(resolver)

            # Mock call_model for the pipeline
            with patch.object(
                resolver.get_active_provider(),
                "call_model",
                side_effect=[
                    json.dumps({"discussion_type": "general", "confidence": 0.7}),
                    json.dumps({
                        "type": "decision_summary",
                        "topic": "Long discussion",
                        "core_problem": "Detailed problem",
                        "proposed_solution": "Comprehensive solution",
                        "requires_code_change": False,
                        "impact_scope": "local",
                        "affected_components": [],
                        "risk_level": "low",
                        "next_steps": []
                    })
                ]
            ):
                client = TestClient(test_app)
                # Create a long message (10KB of text)
                long_text = "This is a detailed technical discussion. " * 250
                response = client.post("/ai/summarize", json={
                    "messages": [
                        {"role": "host", "text": long_text, "timestamp": 1000}
                    ]
                })

                assert response.status_code == 200
                assert response.json()["topic"] == "Long discussion"

        set_resolver(None)

    def test_summarize_fallback_from_bedrock_to_direct(self):
        """Endpoint should work when bedrock fails and falls back to direct."""
        from fastapi.testclient import TestClient
        from app.ai_provider.resolver import ProviderResolver, set_resolver
        from app.ai_provider.router import router
        from fastapi import FastAPI

        test_app = FastAPI()
        test_app.include_router(router)

        # Configure both providers, but bedrock will fail health check
        config = _make_conductor_config(
            enabled=True,
            bedrock_access_key="AKIATEST",
            bedrock_secret_key="secret123",
            anthropic_api_key="sk-ant-test",
        )

        # Mock boto3 to fail
        mock_boto3 = MagicMock()
        mock_boto3.client.return_value.converse.side_effect = Exception("Bedrock unavailable")

        # Mock anthropic to succeed
        mock_anthropic = MagicMock()
        mock_anthropic_client = MagicMock()
        mock_anthropic.Anthropic.return_value = mock_anthropic_client
        mock_anthropic_client.messages.create.return_value = MagicMock()

        with patch.dict("sys.modules", {"boto3": mock_boto3, "anthropic": mock_anthropic}):
            resolver = ProviderResolver(config)
            resolver.resolve()
            set_resolver(resolver)

            # Should have fallen back to anthropic
            assert resolver.active_provider_type == "anthropic"

            # Mock call_model for the pipeline
            with patch.object(
                resolver.get_active_provider(),
                "call_model",
                side_effect=[
                    json.dumps({"discussion_type": "general", "confidence": 0.6}),
                    json.dumps({
                        "type": "decision_summary",
                        "topic": "Fallback test",
                        "core_problem": "Test",
                        "proposed_solution": "Solution",
                        "requires_code_change": False,
                        "impact_scope": "local",
                        "affected_components": [],
                        "risk_level": "low",
                        "next_steps": []
                    })
                ]
            ):
                client = TestClient(test_app)
                response = client.post("/ai/summarize", json={
                    "messages": [{"role": "host", "text": "Test", "timestamp": 1000}]
                })

                assert response.status_code == 200
                assert response.json()["topic"] == "Fallback test"

        set_resolver(None)

    def test_summarize_invalid_message_format(self):
        """Endpoint should return 422 for invalid message format."""
        from fastapi.testclient import TestClient
        from app.ai_provider.router import router
        from fastapi import FastAPI

        test_app = FastAPI()
        test_app.include_router(router)

        client = TestClient(test_app)

        # Missing required fields
        response = client.post("/ai/summarize", json={
            "messages": [{"role": "host"}]  # Missing text and timestamp
        })
        assert response.status_code == 422

        # Invalid role
        response = client.post("/ai/summarize", json={
            "messages": [{"role": "invalid_role", "text": "Hello", "timestamp": 1000}]
        })
        assert response.status_code == 422

    def test_summarize_with_special_characters(self):
        """Endpoint should handle messages with special characters."""
        from fastapi.testclient import TestClient
        from app.ai_provider.resolver import ProviderResolver, set_resolver
        from app.ai_provider.router import router
        from fastapi import FastAPI

        test_app = FastAPI()
        test_app.include_router(router)

        config = _make_conductor_config(enabled=True, anthropic_api_key="sk-ant-test", default_model="claude-sonnet-4-anthropic")

        mock_anthropic = MagicMock()
        mock_client = MagicMock()
        mock_anthropic.Anthropic.return_value = mock_client
        mock_client.messages.create.return_value = MagicMock()

        with patch.dict("sys.modules", {"anthropic": mock_anthropic}):
            resolver = ProviderResolver(config)
            resolver.resolve()
            set_resolver(resolver)

            # Mock call_model for the pipeline
            with patch.object(
                resolver.get_active_provider(),
                "call_model",
                side_effect=[
                    json.dumps({"discussion_type": "debugging", "confidence": 0.85}),
                    json.dumps({
                        "type": "decision_summary",
                        "topic": "Special chars",
                        "core_problem": "Test <script>alert('xss')</script>",
                        "proposed_solution": "Sanitize input",
                        "requires_code_change": True,
                        "impact_scope": "module",
                        "affected_components": ["security.py"],
                        "risk_level": "high",
                        "next_steps": ["Review"]
                    })
                ]
            ):
                client = TestClient(test_app)
                response = client.post("/ai/summarize", json={
                    "messages": [
                        {"role": "host", "text": "Test <script>alert('xss')</script> & \"quotes\"", "timestamp": 1000},
                        {"role": "engineer", "text": "Unicode: 你好 🎉 émojis", "timestamp": 1001}
                    ]
                })

                assert response.status_code == 200

        set_resolver(None)


class TestCodePromptEndpointWithMockProvider:
    """Extended tests for POST /ai/code-prompt with various scenarios."""

    def test_code_prompt_with_all_risk_levels(self):
        """Endpoint should handle all valid risk levels."""
        from fastapi.testclient import TestClient
        from app.ai_provider.router import router
        from fastapi import FastAPI

        test_app = FastAPI()
        test_app.include_router(router)
        client = TestClient(test_app)

        for risk_level in ["low", "medium", "high"]:
            response = client.post("/ai/code-prompt", json={
                "decision_summary": {
                    "type": "decision_summary",
                    "topic": f"Test {risk_level}",
                    "problem_statement": "Problem",
                    "proposed_solution": "Solution",
                    "requires_code_change": True,
                    "affected_components": ["file.py"],
                    "risk_level": risk_level,
                    "next_steps": []
                }
            })
            assert response.status_code == 200
            assert risk_level in response.json()["code_prompt"]

    def test_code_prompt_with_many_components(self):
        """Endpoint should handle many affected components."""
        from fastapi.testclient import TestClient
        from app.ai_provider.router import router
        from fastapi import FastAPI

        test_app = FastAPI()
        test_app.include_router(router)
        client = TestClient(test_app)

        components = [f"module{i}/file{i}.py" for i in range(20)]
        response = client.post("/ai/code-prompt", json={
            "decision_summary": {
                "type": "decision_summary",
                "topic": "Large refactor",
                "problem_statement": "Need to update many files",
                "proposed_solution": "Batch update",
                "requires_code_change": True,
                "affected_components": components,
                "risk_level": "high",
                "next_steps": []
            }
        })

        assert response.status_code == 200
        prompt = response.json()["code_prompt"]
        # Verify some components are in the prompt
        assert "module0/file0.py" in prompt
        assert "module19/file19.py" in prompt

    def test_code_prompt_with_multiline_context(self):
        """Endpoint should handle multiline context snippets."""
        from fastapi.testclient import TestClient
        from app.ai_provider.router import router
        from fastapi import FastAPI

        test_app = FastAPI()
        test_app.include_router(router)
        client = TestClient(test_app)

        context = """def existing_function():
    # This is the current implementation
    result = []
    for item in items:
        if item.is_valid():
            result.append(item.process())
    return result"""

        response = client.post("/ai/code-prompt", json={
            "decision_summary": {
                "type": "decision_summary",
                "topic": "Refactor function",
                "problem_statement": "Function is slow",
                "proposed_solution": "Use list comprehension",
                "requires_code_change": True,
                "affected_components": ["utils.py"],
                "risk_level": "low",
                "next_steps": []
            },
            "context_snippet": context
        })

        assert response.status_code == 200
        prompt = response.json()["code_prompt"]
        assert "def existing_function" in prompt
        assert "list comprehension" in prompt

    def test_code_prompt_without_code_change_required(self):
        """Endpoint should still work when requires_code_change is False."""
        from fastapi.testclient import TestClient
        from app.ai_provider.router import router
        from fastapi import FastAPI

        test_app = FastAPI()
        test_app.include_router(router)
        client = TestClient(test_app)

        response = client.post("/ai/code-prompt", json={
            "decision_summary": {
                "type": "decision_summary",
                "topic": "Documentation update",
                "problem_statement": "Docs are outdated",
                "proposed_solution": "Update README",
                "requires_code_change": False,
                "affected_components": [],
                "risk_level": "low",
                "next_steps": ["Update docs"]
            }
        })

        assert response.status_code == 200
        assert "code_prompt" in response.json()


class TestAIStatusEndpointHealthChecks:
    """Extended tests for GET /ai/status with various health check scenarios."""

    def test_status_with_mixed_provider_health(self):
        """Endpoint should show correct status when providers have mixed health."""
        from fastapi.testclient import TestClient
        from app.ai_provider.resolver import ProviderResolver, set_resolver
        from app.ai_provider.router import router
        from fastapi import FastAPI

        test_app = FastAPI()
        test_app.include_router(router)

        # Configure both providers
        config = _make_conductor_config(
            enabled=True,
            bedrock_access_key="AKIATEST",
            bedrock_secret_key="secret123",
            anthropic_api_key="sk-ant-test",
        )

        # Mock boto3 to fail (bedrock unhealthy)
        mock_boto3 = MagicMock()
        mock_boto3.client.return_value.converse.side_effect = Exception("Bedrock error")

        # Mock anthropic to succeed (direct healthy)
        mock_anthropic = MagicMock()
        mock_anthropic_client = MagicMock()
        mock_anthropic.Anthropic.return_value = mock_anthropic_client
        mock_anthropic_client.messages.create.return_value = MagicMock()

        with patch.dict("sys.modules", {"boto3": mock_boto3, "anthropic": mock_anthropic}):
            resolver = ProviderResolver(config)
            resolver.resolve()
            set_resolver(resolver)

            client = TestClient(test_app)
            response = client.get("/ai/status")

            assert response.status_code == 200
            data = response.json()
            assert data["summary_enabled"] is True
            assert data["active_provider"] == "anthropic"
            assert len(data["providers"]) == 3

            # Find provider statuses
            bedrock_status = next(p for p in data["providers"] if p["name"] == "aws_bedrock")
            direct_status = next(p for p in data["providers"] if p["name"] == "anthropic")

            assert bedrock_status["healthy"] is False
            assert direct_status["healthy"] is True

        set_resolver(None)

    def test_status_with_bedrock_healthy_first(self):
        """Endpoint should select bedrock when it's healthy (priority order)."""
        from fastapi.testclient import TestClient
        from app.ai_provider.resolver import ProviderResolver, set_resolver
        from app.ai_provider.router import router
        from fastapi import FastAPI

        test_app = FastAPI()
        test_app.include_router(router)

        config = _make_conductor_config(
            enabled=True,
            bedrock_access_key="AKIATEST",
            bedrock_secret_key="secret123",
            anthropic_api_key="sk-ant-test",
        )

        # Mock bedrock to succeed
        mock_boto3 = MagicMock()
        mock_boto3.client.return_value.converse.return_value = {"output": {"message": {"content": [{"text": "ok"}]}}}

        mock_anthropic = MagicMock()
        mock_anthropic_client = MagicMock()
        mock_anthropic.Anthropic.return_value = mock_anthropic_client
        mock_anthropic_client.messages.create.return_value = MagicMock()

        with patch.dict("sys.modules", {"boto3": mock_boto3, "anthropic": mock_anthropic}):
            resolver = ProviderResolver(config)
            resolver.resolve()
            set_resolver(resolver)

            client = TestClient(test_app)
            response = client.get("/ai/status")

            assert response.status_code == 200
            data = response.json()
            # Bedrock has priority and is healthy
            assert data["active_provider"] == "aws_bedrock"
            # All provider types are listed
            assert len(data["providers"]) == 3
            # The active provider should be in the list and healthy
            bedrock_status = next((p for p in data["providers"] if p["name"] == "aws_bedrock"), None)
            assert bedrock_status is not None
            assert bedrock_status["healthy"] is True

        set_resolver(None)

    def test_status_with_both_providers_unhealthy(self):
        """Endpoint should show no active provider when all fail health checks."""
        from fastapi.testclient import TestClient
        from app.ai_provider.resolver import ProviderResolver, set_resolver
        from app.ai_provider.router import router
        from fastapi import FastAPI

        test_app = FastAPI()
        test_app.include_router(router)

        config = _make_conductor_config(
            enabled=True,
            bedrock_access_key="AKIATEST",
            bedrock_secret_key="secret123",
            anthropic_api_key="sk-ant-test",
        )

        # Mock both to fail
        mock_boto3 = MagicMock()
        mock_boto3.client.return_value.converse.side_effect = Exception("Bedrock error")

        mock_anthropic = MagicMock()
        mock_anthropic_client = MagicMock()
        mock_anthropic.Anthropic.return_value = mock_anthropic_client
        mock_anthropic_client.messages.create.side_effect = Exception("Direct error")

        with patch.dict("sys.modules", {"boto3": mock_boto3, "anthropic": mock_anthropic}):
            resolver = ProviderResolver(config)
            resolver.resolve()
            set_resolver(resolver)

            client = TestClient(test_app)
            response = client.get("/ai/status")

            assert response.status_code == 200
            data = response.json()
            assert data["summary_enabled"] is True
            assert data["active_provider"] is None
            assert len(data["providers"]) == 3
            # Both configured providers should be unhealthy
            bedrock_status = next(p for p in data["providers"] if p["name"] == "aws_bedrock")
            anthropic_status = next(p for p in data["providers"] if p["name"] == "anthropic")
            assert bedrock_status["healthy"] is False
            assert anthropic_status["healthy"] is False

        set_resolver(None)

    def test_status_returns_consistent_structure(self):
        """Endpoint should always return consistent JSON structure."""
        from fastapi.testclient import TestClient
        from app.ai_provider.resolver import set_resolver
        from app.ai_provider.router import router
        from fastapi import FastAPI

        test_app = FastAPI()
        test_app.include_router(router)

        # Test with no resolver
        set_resolver(None)
        client = TestClient(test_app)
        response = client.get("/ai/status")

        assert response.status_code == 200
        data = response.json()

        # Verify structure
        assert "summary_enabled" in data
        assert "active_provider" in data
        assert "providers" in data
        assert isinstance(data["summary_enabled"], bool)
        assert isinstance(data["providers"], list)


class TestAISummaryPipeline:
    """Tests for the two-stage AI summary pipeline."""

    def test_strip_markdown_code_block(self):
        """Test markdown code block stripping helper."""
        from app.ai_provider.pipeline import _strip_markdown_code_block

        # Test with json code block
        text = '```json\n{"key": "value"}\n```'
        assert _strip_markdown_code_block(text) == '{"key": "value"}'

        # Test with plain code block
        text = '```\n{"key": "value"}\n```'
        assert _strip_markdown_code_block(text) == '{"key": "value"}'

        # Test without code block
        text = '{"key": "value"}'
        assert _strip_markdown_code_block(text) == '{"key": "value"}'

    def test_classify_discussion_empty_messages(self):
        """Test classification with empty messages defaults to general."""
        from app.ai_provider.pipeline import classify_discussion, ClassificationResult

        mock_provider = MagicMock()
        result = classify_discussion([], mock_provider)

        assert isinstance(result, ClassificationResult)
        assert result.discussion_type == "general"
        assert result.confidence == 0.0
        mock_provider.call_model.assert_not_called()

    def test_classify_discussion_success(self):
        """Test successful discussion classification."""
        from app.ai_provider.pipeline import classify_discussion, ClassificationResult
        from app.ai_provider import ChatMessage

        mock_provider = MagicMock()
        mock_provider.call_model.return_value = json.dumps({
            "discussion_type": "api_design",
            "confidence": 0.85
        })

        messages = [
            ChatMessage(role="host", text="Let's design a new REST API", timestamp=1234567890),
            ChatMessage(role="engineer", text="I suggest using POST for creation", timestamp=1234567891),
        ]

        result = classify_discussion(messages, mock_provider)

        assert isinstance(result, ClassificationResult)
        assert result.discussion_type == "api_design"
        assert result.confidence == 0.85
        mock_provider.call_model.assert_called_once()

    def test_classify_discussion_with_markdown_wrapper(self):
        """Test classification handles markdown-wrapped response."""
        from app.ai_provider.pipeline import classify_discussion
        from app.ai_provider import ChatMessage

        mock_provider = MagicMock()
        mock_provider.call_model.return_value = '```json\n{"discussion_type": "debugging", "confidence": 0.9}\n```'

        messages = [ChatMessage(role="host", text="The app is crashing", timestamp=1234567890)]
        result = classify_discussion(messages, mock_provider)

        assert result.discussion_type == "debugging"
        assert result.confidence == 0.9

    def test_classify_discussion_invalid_type_defaults_to_general(self):
        """Test classification defaults to general for invalid types."""
        from app.ai_provider.pipeline import classify_discussion
        from app.ai_provider import ChatMessage

        mock_provider = MagicMock()
        mock_provider.call_model.return_value = json.dumps({
            "discussion_type": "unknown_type",
            "confidence": 0.5
        })

        messages = [ChatMessage(role="host", text="Hello", timestamp=1234567890)]
        result = classify_discussion(messages, mock_provider)

        assert result.discussion_type == "general"

    def test_classify_discussion_invalid_json_raises_error(self):
        """Test classification raises ValueError for invalid JSON."""
        from app.ai_provider.pipeline import classify_discussion
        from app.ai_provider import ChatMessage

        mock_provider = MagicMock()
        mock_provider.call_model.return_value = "not valid json"

        messages = [ChatMessage(role="host", text="Hello", timestamp=1234567890)]

        with pytest.raises(ValueError, match="Invalid JSON"):
            classify_discussion(messages, mock_provider)

    def test_generate_targeted_summary_empty_messages(self):
        """Test targeted summary with empty messages returns default."""
        from app.ai_provider.pipeline import generate_targeted_summary, PipelineSummary

        mock_provider = MagicMock()
        result = generate_targeted_summary([], mock_provider, "general")

        assert isinstance(result, PipelineSummary)
        assert result.discussion_type == "general"
        mock_provider.call_model.assert_not_called()

    def test_generate_targeted_summary_success(self):
        """Test successful targeted summary generation."""
        from app.ai_provider.pipeline import generate_targeted_summary, PipelineSummary
        from app.ai_provider import ChatMessage

        mock_provider = MagicMock()
        mock_provider.call_model.return_value = json.dumps({
            "type": "decision_summary",
            "topic": "API Design Discussion",
            "core_problem": "Need new endpoints",
            "proposed_solution": "Create REST API",
            "requires_code_change": True,
            "impact_scope": "module",
            "affected_components": ["api.py", "router.py"],
            "risk_level": "medium",
            "next_steps": ["Design endpoints", "Write tests"]
        })

        messages = [
            ChatMessage(role="host", text="Let's design the API", timestamp=1234567890),
        ]

        result = generate_targeted_summary(messages, mock_provider, "api_design")

        assert isinstance(result, PipelineSummary)
        assert result.topic == "API Design Discussion"
        assert result.core_problem == "Need new endpoints"
        assert result.proposed_solution == "Create REST API"
        assert result.requires_code_change is True
        assert result.impact_scope == "module"
        assert result.affected_components == ["api.py", "router.py"]
        assert result.risk_level == "medium"
        assert result.next_steps == ["Design endpoints", "Write tests"]
        assert result.discussion_type == "api_design"

    def test_generate_targeted_summary_invalid_json_raises_error(self):
        """Test targeted summary raises ValueError for invalid JSON."""
        from app.ai_provider.pipeline import generate_targeted_summary
        from app.ai_provider import ChatMessage

        mock_provider = MagicMock()
        mock_provider.call_model.return_value = "not valid json"

        messages = [ChatMessage(role="host", text="Hello", timestamp=1234567890)]

        with pytest.raises(ValueError, match="Invalid JSON"):
            generate_targeted_summary(messages, mock_provider, "general")

    def test_infer_requires_code_change_for_code_change_type(self):
        """Test code_change type always requires code change."""
        from app.ai_provider.pipeline import _infer_requires_code_change

        # Even if AI says false, code_change type should be true
        data = {"requires_code_change": False}
        assert _infer_requires_code_change(data, "code_change") is True

    def test_infer_requires_code_change_for_debugging_with_solution(self):
        """Test debugging with solution requires code change."""
        from app.ai_provider.pipeline import _infer_requires_code_change

        data = {
            "requires_code_change": False,
            "proposed_solution": "We need to fix this bug by adding null checks in the handler"
        }
        assert _infer_requires_code_change(data, "debugging") is True

    def test_infer_requires_code_change_for_api_design_with_components(self):
        """Test api_design with components requires code change."""
        from app.ai_provider.pipeline import _infer_requires_code_change

        data = {
            "requires_code_change": False,
            "affected_components": ["api.py"]
        }
        assert _infer_requires_code_change(data, "api_design") is True

    def test_infer_requires_code_change_respects_ai_for_other_types(self):
        """Test general type respects AI assessment."""
        from app.ai_provider.pipeline import _infer_requires_code_change

        data = {"requires_code_change": False}
        assert _infer_requires_code_change(data, "general") is False

        data = {"requires_code_change": True}
        assert _infer_requires_code_change(data, "general") is True

    def test_run_summary_pipeline_complete(self):
        """Test complete pipeline execution."""
        from app.ai_provider.pipeline import run_summary_pipeline, PipelineSummary
        from app.ai_provider import ChatMessage

        mock_provider = MagicMock()
        # First call for classification, second for summary, third for item extraction
        mock_provider.call_model.side_effect = [
            json.dumps({"discussion_type": "code_change", "confidence": 0.95}),
            json.dumps({
                "type": "decision_summary",
                "topic": "Bug Fix",
                "core_problem": "Null pointer error",
                "proposed_solution": "Add null check",
                "requires_code_change": True,
                "impact_scope": "local",
                "affected_components": ["handler.py"],
                "risk_level": "low",
                "next_steps": ["Fix the bug"]
            }),
            json.dumps([{
                "id": "item-1",
                "type": "code_change",
                "title": "Add null check",
                "problem": "Null pointer error",
                "proposed_change": "Add null check in handler",
                "targets": ["handler.py"],
                "risk_level": "low",
            }])
        ]

        messages = [
            ChatMessage(role="host", text="There's a bug in handler.py", timestamp=1234567890),
            ChatMessage(role="engineer", text="I'll add a null check", timestamp=1234567891),
        ]

        result = run_summary_pipeline(messages, mock_provider)

        assert isinstance(result, PipelineSummary)
        assert result.discussion_type == "code_change"
        assert result.classification_confidence == 0.95
        assert result.topic == "Bug Fix"
        assert result.requires_code_change is True  # code_change type always true
        assert mock_provider.call_model.call_count == 3  # classification + summary + items
        assert len(result.code_relevant_items) == 1

    def test_pipeline_summary_dataclass_defaults(self):
        """Test PipelineSummary dataclass default values."""
        from app.ai_provider.pipeline import PipelineSummary

        summary = PipelineSummary()

        assert summary.type == "decision_summary"
        assert summary.topic == ""
        assert summary.core_problem == ""
        assert summary.proposed_solution == ""
        assert summary.requires_code_change is False
        assert summary.impact_scope == "local"
        assert summary.affected_components == []
        assert summary.risk_level == "low"
        assert summary.next_steps == []
        assert summary.discussion_type == "general"
        assert summary.classification_confidence == 0.0

    def test_classification_result_dataclass(self):
        """Test ClassificationResult dataclass."""
        from app.ai_provider.pipeline import ClassificationResult

        result = ClassificationResult(discussion_type="api_design", confidence=0.9)

        assert result.discussion_type == "api_design"
        assert result.confidence == 0.9


class TestFormatConversationXml:
    """Tests for the XML-formatted conversation output."""

    def test_format_conversation_xml_structure(self):
        """format_conversation should produce XML message tags."""
        from app.ai_provider.prompts import format_conversation
        from app.ai_provider import ChatMessage

        messages = [
            ChatMessage(role="host", text="Hello team", timestamp=1234567890),
            ChatMessage(role="engineer", text="Hi there", timestamp=1234567891),
        ]

        result = format_conversation(messages)

        assert '<message role="host">Hello team</message>' in result
        assert '<message role="engineer">Hi there</message>' in result
        # Old format should NOT be present
        assert "[Host]" not in result
        assert "[Engineer]" not in result

    def test_format_conversation_empty_messages(self):
        """format_conversation should handle empty list."""
        from app.ai_provider.prompts import format_conversation

        result = format_conversation([])
        assert result == "(No messages in conversation)"

    def test_format_conversation_single_message(self):
        """format_conversation should handle a single message."""
        from app.ai_provider.prompts import format_conversation
        from app.ai_provider import ChatMessage

        messages = [ChatMessage(role="host", text="Just me", timestamp=1234567890)]
        result = format_conversation(messages)

        assert '<message role="host">Just me</message>' in result
        assert result.count("<message") == 1


class TestGetCodePromptWithPolicyAndStyle:
    """Tests for get_code_prompt with policy and style injection."""

    def test_get_code_prompt_with_policy(self):
        """Code prompt should include policy_constraints XML block."""
        from app.ai_provider.prompts import get_code_prompt

        prompt = get_code_prompt(
            problem_statement="Fix login",
            proposed_solution="Add validation",
            affected_components=["auth.py"],
            risk_level="low",
            policy_constraints="- Maximum files that may be changed: 2\n- Maximum total lines changed: 50",
        )

        assert "<policy_constraints>" in prompt
        assert "Maximum files that may be changed: 2" in prompt
        assert "</policy_constraints>" in prompt

    def test_get_code_prompt_with_style(self):
        """Code prompt should include code_style XML block."""
        from app.ai_provider.prompts import get_code_prompt

        prompt = get_code_prompt(
            problem_statement="Fix login",
            proposed_solution="Add validation",
            affected_components=["auth.py"],
            risk_level="low",
            style_guidelines="Use 4-space indentation. Follow PEP 8.",
        )

        assert "<code_style>" in prompt
        assert "Use 4-space indentation" in prompt
        assert "</code_style>" in prompt

    def test_get_code_prompt_with_both(self):
        """Code prompt should include both policy and style blocks."""
        from app.ai_provider.prompts import get_code_prompt

        prompt = get_code_prompt(
            problem_statement="Fix login",
            proposed_solution="Add validation",
            affected_components=["auth.py"],
            risk_level="low",
            policy_constraints="- Max files: 2",
            style_guidelines="Use PEP 8",
        )

        assert "<policy_constraints>" in prompt
        assert "<code_style>" in prompt
        # Both should appear before the instructions block
        policy_pos = prompt.index("<policy_constraints>")
        style_pos = prompt.index("<code_style>")
        instructions_pos = prompt.index("<instructions>")
        assert policy_pos < instructions_pos
        assert style_pos < instructions_pos

    def test_get_code_prompt_without_policy_or_style(self):
        """Code prompt should not include policy/style blocks when not provided."""
        from app.ai_provider.prompts import get_code_prompt

        prompt = get_code_prompt(
            problem_statement="Fix login",
            proposed_solution="Add validation",
            affected_components=["auth.py"],
            risk_level="low",
        )

        assert "<policy_constraints>" not in prompt
        assert "<code_style>" not in prompt

    def test_get_code_prompt_xml_structure(self):
        """Code prompt should use XML tags for problem, solution, components."""
        from app.ai_provider.prompts import get_code_prompt

        prompt = get_code_prompt(
            problem_statement="Users cannot log in",
            proposed_solution="Add authentication",
            affected_components=["auth.py"],
            risk_level="medium",
        )

        assert "<problem>" in prompt
        assert "</problem>" in prompt
        assert "<solution>" in prompt
        assert "</solution>" in prompt
        assert "<target_components>" in prompt
        assert "</target_components>" in prompt


class TestFormatPolicyConstraints:
    """Tests for the format_policy_constraints helper."""

    def test_basic_constraints(self):
        """Should format max_files and max_lines."""
        from app.ai_provider.prompts import format_policy_constraints

        result = format_policy_constraints(max_files=2, max_lines_changed=50)

        assert "Maximum files that may be changed: 2" in result
        assert "Maximum total lines changed: 50" in result

    def test_with_forbidden_paths(self):
        """Should include forbidden paths when provided."""
        from app.ai_provider.prompts import format_policy_constraints

        result = format_policy_constraints(
            max_files=5,
            max_lines_changed=100,
            forbidden_paths=("infra/", "db/", "security/"),
        )

        assert "infra/" in result
        assert "db/" in result
        assert "security/" in result
        assert "Do NOT modify" in result

    def test_without_forbidden_paths(self):
        """Should not include forbidden line when paths tuple is empty."""
        from app.ai_provider.prompts import format_policy_constraints

        result = format_policy_constraints(max_files=10, max_lines_changed=500)

        assert "Do NOT modify" not in result


class TestSelectiveCodePromptWithPolicyAndStyle:
    """Tests for get_selective_code_prompt with policy and style injection."""

    def test_selective_prompt_xml_summaries(self):
        """Selective code prompt should format summaries with XML tags."""
        from app.ai_provider.prompts import get_selective_code_prompt

        summaries = [
            {
                "discussion_type": "code_change",
                "topic": "Fix auth",
                "core_problem": "Login broken",
                "proposed_solution": "Add validation",
                "affected_components": ["auth.py"],
                "risk_level": "medium",
                "next_steps": ["Write tests"],
            }
        ]

        prompt = get_selective_code_prompt(
            primary_focus="Fix authentication",
            impact_scope="module",
            summaries=summaries,
        )

        assert "<summaries>" in prompt
        assert "</summaries>" in prompt
        assert '<summary index="1" type="code_change">' in prompt
        assert "<primary_focus>" in prompt
        assert "<impact_scope>" in prompt

    def test_selective_prompt_with_policy_and_style(self):
        """Selective code prompt should accept policy/style params."""
        from app.ai_provider.prompts import get_selective_code_prompt

        prompt = get_selective_code_prompt(
            primary_focus="Fix auth",
            impact_scope="local",
            summaries=[],
            policy_constraints="- Max files: 2",
            style_guidelines="Follow PEP 8",
        )

        # These params are accepted but only used in the system prompt
        assert isinstance(prompt, str)


class TestLoadStyleGuidelinesBugFix:
    """Tests for _load_style_guidelines returning str not tuple."""

    def test_load_style_guidelines_returns_string(self):
        """_load_style_guidelines() should return a string, not a tuple."""
        from app.ai_provider.wrapper import _load_style_guidelines

        result = _load_style_guidelines(room_code_style=None)
        # Should be a string (the universal style content), not a tuple
        assert result is None or isinstance(result, str), (
            f"Expected str or None, got {type(result)}: {repr(result)[:100]}"
        )

    def test_load_style_guidelines_with_room_style(self):
        """_load_style_guidelines() with room style should return it directly."""
        from app.ai_provider.wrapper import _load_style_guidelines

        result = _load_style_guidelines(room_code_style="Use PEP 8")
        assert result == "Use PEP 8"

    def test_load_style_guidelines_fallback_is_not_tuple(self):
        """When no room style, fallback should not be a stringified tuple."""
        from app.ai_provider.wrapper import _load_style_guidelines

        result = _load_style_guidelines(room_code_style=None)
        if result is not None:
            assert not result.startswith("("), "Result looks like a stringified tuple"
            assert "StyleSource" not in result, "Result contains StyleSource enum text"


class TestStyleTemplatesEndpoint:
    """Tests for GET /ai/style-templates endpoint."""

    def test_style_templates_returns_200(self):
        """GET /ai/style-templates should return 200 with template list."""
        from fastapi.testclient import TestClient
        from app.ai_provider.router import router
        from fastapi import FastAPI

        test_app = FastAPI()
        test_app.include_router(router)

        client = TestClient(test_app)
        response = client.get("/ai/style-templates")

        assert response.status_code == 200
        data = response.json()
        assert "templates" in data
        assert isinstance(data["templates"], list)
        assert len(data["templates"]) >= 6

    def test_style_templates_have_expected_fields(self):
        """Each template should have name, filename, and content fields."""
        from fastapi.testclient import TestClient
        from app.ai_provider.router import router
        from fastapi import FastAPI

        test_app = FastAPI()
        test_app.include_router(router)

        client = TestClient(test_app)
        response = client.get("/ai/style-templates")

        data = response.json()
        for t in data["templates"]:
            assert "name" in t
            assert "filename" in t
            assert "content" in t
            assert len(t["content"]) > 0

    def test_style_templates_include_universal(self):
        """Templates should include the universal style."""
        from fastapi.testclient import TestClient
        from app.ai_provider.router import router
        from fastapi import FastAPI

        test_app = FastAPI()
        test_app.include_router(router)

        client = TestClient(test_app)
        response = client.get("/ai/style-templates")

        data = response.json()
        names = [t["name"] for t in data["templates"]]
        assert "universal" in names


class TestLoadStyleGuidelinesWithDetectedLanguages:
    """Tests for _load_style_guidelines with detected_languages parameter."""

    def test_load_style_guidelines_with_detected_languages(self):
        """Passing detected languages should return universal + language styles."""
        from app.ai_provider.wrapper import _load_style_guidelines

        result = _load_style_guidelines(
            room_code_style=None,
            detected_languages=["python", "javascript"],
        )
        assert result is not None
        assert isinstance(result, str)
        # Should contain universal content
        assert "---" in result  # separator between sections
        # Should contain Python style content
        assert "python" in result.lower() or "Python" in result
        # Should contain JavaScript style content
        assert "javascript" in result.lower() or "JavaScript" in result

    def test_load_style_guidelines_with_empty_languages(self):
        """Empty detected_languages should fall back to universal only."""
        from app.ai_provider.wrapper import _load_style_guidelines

        result = _load_style_guidelines(
            room_code_style=None,
            detected_languages=[],
        )
        # Empty list is falsy, so should fall back to CodeStyleLoader (universal)
        assert result is None or isinstance(result, str)
        if result is not None:
            # Should not contain language-specific separator
            assert "---" not in result or "python" not in result.lower()

    def test_load_style_guidelines_with_invalid_language(self):
        """Invalid language strings should be silently skipped."""
        from app.ai_provider.wrapper import _load_style_guidelines

        result = _load_style_guidelines(
            room_code_style=None,
            detected_languages=["rust"],
        )
        assert result is not None
        assert isinstance(result, str)
        # Should still return universal style even though "rust" is invalid
        assert "rust" not in result.lower()

    def test_load_style_guidelines_room_style_overrides_languages(self):
        """Room code style should take precedence over detected languages."""
        from app.ai_provider.wrapper import _load_style_guidelines

        result = _load_style_guidelines(
            room_code_style="Use PEP 8 everywhere",
            detected_languages=["python", "javascript"],
        )
        assert result == "Use PEP 8 everywhere"

    def test_load_style_guidelines_mixed_valid_invalid_languages(self):
        """Valid languages should be included, invalid ones silently skipped."""
        from app.ai_provider.wrapper import _load_style_guidelines

        result = _load_style_guidelines(
            room_code_style=None,
            detected_languages=["python", "rust", "cobol"],
        )
        assert result is not None
        assert isinstance(result, str)
        # Should contain Python style content
        assert "python" in result.lower() or "Python" in result
        # Should contain separator (universal + python)
        assert "---" in result


class TestCodePromptEndpointWithDetectedLanguages:
    """Tests for code-prompt endpoints with detected_languages field."""

    def test_code_prompt_endpoint_with_detected_languages(self):
        """POST /ai/code-prompt with detected_languages should return 200."""
        from fastapi.testclient import TestClient
        from app.ai_provider.router import router
        from fastapi import FastAPI

        test_app = FastAPI()
        test_app.include_router(router)

        client = TestClient(test_app)
        response = client.post("/ai/code-prompt", json={
            "decision_summary": {
                "type": "decision_summary",
                "topic": "Add user authentication",
                "problem_statement": "Users cannot log in securely",
                "proposed_solution": "Implement JWT-based authentication",
                "requires_code_change": True,
                "affected_components": ["auth/login.py"],
                "risk_level": "medium",
                "next_steps": ["Implement login endpoint"]
            },
            "detected_languages": ["python"]
        })

        assert response.status_code == 200
        data = response.json()
        assert "code_prompt" in data
        # The prompt should contain Python style content
        prompt = data["code_prompt"]
        assert len(prompt) > 0

    def test_code_prompt_endpoint_backward_compatible(self):
        """POST /ai/code-prompt without detected_languages should still return 200."""
        from fastapi.testclient import TestClient
        from app.ai_provider.router import router
        from fastapi import FastAPI

        test_app = FastAPI()
        test_app.include_router(router)

        client = TestClient(test_app)
        response = client.post("/ai/code-prompt", json={
            "decision_summary": {
                "type": "decision_summary",
                "topic": "Fix login bug",
                "problem_statement": "Login fails silently",
                "proposed_solution": "Add error handling",
                "requires_code_change": True,
                "affected_components": ["auth/login.py"],
                "risk_level": "low",
                "next_steps": ["Add try-catch"]
            }
        })

        assert response.status_code == 200
        data = response.json()
        assert "code_prompt" in data


# =============================================================================
# TestExtractCodeRelevantItems
# =============================================================================


class TestExtractCodeRelevantItems:
    """Tests for extract_code_relevant_items() - pipeline stage 4."""

    def _make_summary(self, **kwargs):
        from app.ai_provider.pipeline import PipelineSummary
        defaults = {
            "topic": "Add user auth",
            "core_problem": "No auth exists",
            "proposed_solution": "Add JWT-based auth",
            "requires_code_change": True,
            "affected_components": ["auth/login.py"],
            "risk_level": "medium",
            "next_steps": ["Create login endpoint"],
            "discussion_type": "code_change",
        }
        defaults.update(kwargs)
        return PipelineSummary(**defaults)

    def test_single_item_extraction(self):
        """Should extract a single item from AI response."""
        from app.ai_provider.pipeline import extract_code_relevant_items

        mock_provider = MagicMock()
        mock_provider.call_model.return_value = json.dumps([
            {
                "id": "item-1",
                "type": "code_change",
                "title": "Add login endpoint",
                "problem": "No auth endpoint",
                "proposed_change": "Create POST /auth/login",
                "targets": ["auth/login.py"],
                "risk_level": "medium",
            }
        ])

        summary = self._make_summary()
        items = extract_code_relevant_items(summary, mock_provider)

        assert len(items) == 1
        assert items[0].id == "item-1"
        assert items[0].type == "code_change"
        assert items[0].title == "Add login endpoint"
        assert items[0].targets == ["auth/login.py"]
        assert items[0].risk_level == "medium"

    def test_multiple_items_extraction(self):
        """Should extract multiple items and assign sequential IDs."""
        from app.ai_provider.pipeline import extract_code_relevant_items

        mock_provider = MagicMock()
        mock_provider.call_model.return_value = json.dumps([
            {
                "type": "api_design",
                "title": "Create endpoint",
                "problem": "No endpoint",
                "proposed_change": "Add POST /users",
                "targets": ["api/users.py"],
                "risk_level": "low",
            },
            {
                "type": "code_change",
                "title": "Add middleware",
                "problem": "No auth check",
                "proposed_change": "Add auth middleware",
                "targets": ["middleware/auth.py"],
                "risk_level": "medium",
            },
        ])

        summary = self._make_summary()
        items = extract_code_relevant_items(summary, mock_provider)

        assert len(items) == 2
        assert items[0].id == "item-1"
        assert items[1].id == "item-2"
        assert items[0].type == "api_design"
        assert items[1].type == "code_change"

    def test_invalid_json_raises_value_error(self):
        """Should raise ValueError when AI returns invalid JSON."""
        from app.ai_provider.pipeline import extract_code_relevant_items

        mock_provider = MagicMock()
        mock_provider.call_model.return_value = "not valid json"

        summary = self._make_summary()
        with pytest.raises(ValueError, match="Invalid JSON"):
            extract_code_relevant_items(summary, mock_provider)

    def test_markdown_wrapped_response(self):
        """Should handle markdown-wrapped JSON responses."""
        from app.ai_provider.pipeline import extract_code_relevant_items

        items_json = json.dumps([{
            "id": "item-1",
            "type": "code_change",
            "title": "Fix bug",
            "problem": "Bug exists",
            "proposed_change": "Fix it",
            "targets": ["app.py"],
            "risk_level": "low",
        }])
        mock_provider = MagicMock()
        mock_provider.call_model.return_value = f"```json\n{items_json}\n```"

        summary = self._make_summary()
        items = extract_code_relevant_items(summary, mock_provider)

        assert len(items) == 1
        assert items[0].title == "Fix bug"

    def test_sequential_id_assignment(self):
        """Should assign sequential IDs even when AI omits them."""
        from app.ai_provider.pipeline import extract_code_relevant_items

        mock_provider = MagicMock()
        mock_provider.call_model.return_value = json.dumps([
            {"type": "code_change", "title": "A", "problem": "", "proposed_change": "", "targets": [], "risk_level": "low"},
            {"type": "code_change", "title": "B", "problem": "", "proposed_change": "", "targets": [], "risk_level": "low"},
            {"type": "code_change", "title": "C", "problem": "", "proposed_change": "", "targets": [], "risk_level": "low"},
        ])

        summary = self._make_summary()
        items = extract_code_relevant_items(summary, mock_provider)

        assert [i.id for i in items] == ["item-1", "item-2", "item-3"]

    def test_invalid_type_defaults_to_code_change(self):
        """Should default invalid types to code_change."""
        from app.ai_provider.pipeline import extract_code_relevant_items

        mock_provider = MagicMock()
        mock_provider.call_model.return_value = json.dumps([{
            "type": "invalid_type",
            "title": "Test",
            "problem": "",
            "proposed_change": "",
            "targets": [],
            "risk_level": "low",
        }])

        summary = self._make_summary()
        items = extract_code_relevant_items(summary, mock_provider)

        assert items[0].type == "code_change"

    def test_invalid_risk_defaults_to_low(self):
        """Should default invalid risk levels to low."""
        from app.ai_provider.pipeline import extract_code_relevant_items

        mock_provider = MagicMock()
        mock_provider.call_model.return_value = json.dumps([{
            "type": "code_change",
            "title": "Test",
            "problem": "",
            "proposed_change": "",
            "targets": [],
            "risk_level": "critical",
        }])

        summary = self._make_summary()
        items = extract_code_relevant_items(summary, mock_provider)

        assert items[0].risk_level == "low"


# =============================================================================
# TestPipelineWithItems
# =============================================================================


class TestPipelineWithItems:
    """Tests for run_summary_pipeline() with stage 4 item extraction."""

    def test_pipeline_produces_items_when_code_change_required(self):
        """Pipeline should produce code_relevant_items when requires_code_change is True."""
        from app.ai_provider.pipeline import run_summary_pipeline

        mock_provider = MagicMock()
        mock_provider.call_model.side_effect = [
            # Stage 1: classification
            json.dumps({"discussion_type": "code_change", "confidence": 0.9}),
            # Stage 2: targeted summary
            json.dumps({
                "topic": "Add auth",
                "core_problem": "No auth",
                "proposed_solution": "Add JWT",
                "requires_code_change": True,
                "impact_scope": "module",
                "affected_components": ["auth.py"],
                "risk_level": "medium",
                "next_steps": ["Add endpoint"],
            }),
            # Stage 4: item extraction
            json.dumps([{
                "id": "item-1",
                "type": "code_change",
                "title": "Add auth endpoint",
                "problem": "No auth",
                "proposed_change": "Create POST /auth",
                "targets": ["auth.py"],
                "risk_level": "medium",
            }]),
        ]

        messages = [MagicMock(role="host", text="Add auth", timestamp=1234567890)]
        result = run_summary_pipeline(messages, mock_provider)

        assert len(result.code_relevant_items) == 1
        assert result.code_relevant_items[0].title == "Add auth endpoint"
        # call_model called 3 times: classification, summary, items
        assert mock_provider.call_model.call_count == 3

    def test_pipeline_fallback_on_stage4_failure(self):
        """Pipeline should use fallback item when stage 4 fails and requires_code_change is True."""
        from app.ai_provider.pipeline import run_summary_pipeline

        mock_provider = MagicMock()
        mock_provider.call_model.side_effect = [
            # Stage 1: classification
            json.dumps({"discussion_type": "code_change", "confidence": 0.9}),
            # Stage 2: targeted summary
            json.dumps({
                "topic": "Fix bug",
                "core_problem": "Bug in auth",
                "proposed_solution": "Patch the bug",
                "requires_code_change": True,
                "impact_scope": "local",
                "affected_components": ["auth.py"],
                "risk_level": "low",
                "next_steps": ["Fix it"],
            }),
            # Stage 4: fails
            Exception("AI failure"),
        ]

        messages = [MagicMock(role="host", text="Fix bug", timestamp=1234567890)]
        result = run_summary_pipeline(messages, mock_provider)

        # Should have fallback item
        assert len(result.code_relevant_items) == 1
        assert result.code_relevant_items[0].id == "item-1"
        assert result.code_relevant_items[0].title == "Fix bug"
        assert result.code_relevant_items[0].targets == ["auth.py"]

    def test_pipeline_skips_stage4_when_no_code_change(self):
        """Pipeline should skip stage 4 when no code change is needed."""
        from app.ai_provider.pipeline import run_summary_pipeline

        mock_provider = MagicMock()
        mock_provider.call_model.side_effect = [
            # Stage 1: classification
            json.dumps({"discussion_type": "general", "confidence": 0.7}),
            # Stage 2: targeted summary
            json.dumps({
                "topic": "Team standup",
                "core_problem": "Weekly planning",
                "proposed_solution": "Continue current sprint",
                "requires_code_change": False,
                "impact_scope": "local",
                "affected_components": [],
                "risk_level": "low",
                "next_steps": ["Continue work"],
            }),
        ]

        messages = [MagicMock(role="host", text="Standup", timestamp=1234567890)]
        result = run_summary_pipeline(messages, mock_provider)

        assert result.code_relevant_items == []
        # call_model called only 2 times: classification, summary (no stage 4)
        assert mock_provider.call_model.call_count == 2