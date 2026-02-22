"""Tests for the /embeddings endpoint and supporting layers.

All tests mock the Bedrock client so no real AWS credentials are needed.
"""
import json
from io import BytesIO
from typing import Generator
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.embeddings.bedrock import BedrockEmbeddingProvider
from app.embeddings.schemas import MAX_BATCH
from app.embeddings.service import EmbeddingService, get_embedding_service, set_embedding_service
from app.main import app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_bedrock_response(vectors: list[list[float]]) -> dict:
    """Build a fake boto3 invoke_model response with a Cohere-style body."""
    body = json.dumps({"embeddings": vectors}).encode()
    return {"body": BytesIO(body)}


def _make_bedrock_response_nested(vectors: list[list[float]]) -> dict:
    """Nested format: { "embeddings": { "float": [...] } }."""
    body = json.dumps({"embeddings": {"float": vectors}}).encode()
    return {"body": BytesIO(body)}


DIM = 4
SAMPLE_VECTOR = [0.1, -0.2, 0.3, 0.4]


# ---------------------------------------------------------------------------
# BedrockEmbeddingProvider unit tests
# ---------------------------------------------------------------------------

class TestBedrockEmbeddingProvider:
    def _provider(self) -> BedrockEmbeddingProvider:
        return BedrockEmbeddingProvider(
            model_id="cohere.embed-v4",
            dim=DIM,
            aws_access_key_id="FAKE_KEY",
            aws_secret_access_key="FAKE_SECRET",
        )

    def test_model_id_property(self):
        p = self._provider()
        assert p.model_id == "cohere.embed-v4"

    def test_dim_property(self):
        p = self._provider()
        assert p.dim == DIM

    def test_embed_flat_format(self):
        """Provider parses the flat embeddings response correctly."""
        p = self._provider()
        mock_client = MagicMock()
        mock_client.invoke_model.return_value = _make_bedrock_response([SAMPLE_VECTOR])
        p._client = mock_client

        result = p.embed(["hello"])
        assert result == [SAMPLE_VECTOR]
        mock_client.invoke_model.assert_called_once()

    def test_embed_nested_format(self):
        """Provider also handles the nested { 'float': [...] } response."""
        p = self._provider()
        mock_client = MagicMock()
        mock_client.invoke_model.return_value = _make_bedrock_response_nested([SAMPLE_VECTOR])
        p._client = mock_client

        result = p.embed(["hello"])
        assert result == [SAMPLE_VECTOR]

    def test_embed_batch(self):
        """Provider returns one vector per input text."""
        texts = ["text one", "text two", "text three"]
        vectors = [SAMPLE_VECTOR, SAMPLE_VECTOR, SAMPLE_VECTOR]
        p = self._provider()
        mock_client = MagicMock()
        mock_client.invoke_model.return_value = _make_bedrock_response(vectors)
        p._client = mock_client

        result = p.embed(texts)
        assert len(result) == 3

    def test_embed_passes_correct_body(self):
        """Request body matches Cohere Embed schema."""
        p = self._provider()
        mock_client = MagicMock()
        mock_client.invoke_model.return_value = _make_bedrock_response([SAMPLE_VECTOR])
        p._client = mock_client

        p.embed(["my code snippet"])

        call_kwargs = mock_client.invoke_model.call_args[1]
        body = json.loads(call_kwargs["body"])
        assert body["texts"] == ["my code snippet"]
        assert body["input_type"] == "search_document"
        assert body["truncate"] == "END"
        assert call_kwargs["modelId"] == "cohere.embed-v4"

    def test_embed_raises_on_missing_embeddings_key(self):
        """Malformed response triggers ValueError."""
        p = self._provider()
        mock_client = MagicMock()
        mock_client.invoke_model.return_value = {"body": BytesIO(b"{}")}
        p._client = mock_client

        with pytest.raises(ValueError, match="'embeddings' key missing"):
            p.embed(["x"])

    def test_embed_raises_on_count_mismatch(self):
        """Provider raises if vector count != text count."""
        p = self._provider()
        mock_client = MagicMock()
        # Send 2 texts but return only 1 vector
        mock_client.invoke_model.return_value = _make_bedrock_response([SAMPLE_VECTOR])
        p._client = mock_client

        with pytest.raises(ValueError, match="2 texts"):
            p.embed(["a", "b"])


# ---------------------------------------------------------------------------
# EmbeddingService unit tests
# ---------------------------------------------------------------------------

class TestEmbeddingService:
    def _service(self, vectors: list[list[float]] | None = None) -> EmbeddingService:
        provider = MagicMock()
        provider.model_id = "test-model"
        provider.dim = DIM
        provider.embed.return_value = vectors or [SAMPLE_VECTOR]
        return EmbeddingService(provider)

    def test_embed_delegates_to_provider(self):
        svc = self._service([SAMPLE_VECTOR])
        result = svc.embed(["hello"])
        assert result == [SAMPLE_VECTOR]

    def test_embed_raises_on_empty_texts(self):
        svc = self._service()
        with pytest.raises(ValueError, match="empty"):
            svc.embed([])

    def test_embed_raises_when_batch_too_large(self):
        svc = self._service()
        with pytest.raises(ValueError, match=str(MAX_BATCH)):
            svc.embed(["x"] * (MAX_BATCH + 1))

    def test_model_id_forwarded(self):
        svc = self._service()
        assert svc.model_id == "test-model"

    def test_dim_forwarded(self):
        svc = self._service()
        assert svc.dim == DIM


# ---------------------------------------------------------------------------
# POST /embeddings endpoint tests
# ---------------------------------------------------------------------------

@pytest.fixture()
def client() -> Generator[TestClient, None, None]:
    yield TestClient(app)


@pytest.fixture(autouse=True)
def reset_service():
    """Ensure a clean service singleton for each test."""
    original = get_embedding_service()
    yield
    set_embedding_service(original)  # type: ignore[arg-type]


@pytest.fixture()
def mock_service() -> EmbeddingService:
    provider = MagicMock()
    provider.model_id = "cohere.embed-v4"
    provider.dim = 1024
    provider.embed.return_value = [[float(i) for i in range(1024)]]
    svc = EmbeddingService(provider)
    set_embedding_service(svc)
    return svc


class TestEmbedEndpoint:
    def test_returns_503_when_service_not_configured(self, client: TestClient):
        set_embedding_service(None)  # type: ignore[arg-type]
        resp = client.post("/embeddings", json={"texts": ["hello"]})
        assert resp.status_code == 503

    def test_successful_embed(self, client: TestClient, mock_service: EmbeddingService):
        resp = client.post("/embeddings", json={"texts": ["function greet(): void"]})
        assert resp.status_code == 200
        data = resp.json()
        assert "vectors" in data
        assert len(data["vectors"]) == 1
        assert len(data["vectors"][0]) == 1024
        assert data["model"] == "cohere.embed-v4"
        assert data["dim"] == 1024

    def test_batch_of_multiple_texts(self, client: TestClient, mock_service: EmbeddingService):
        mock_service._provider.embed.return_value = [
            [float(i) for i in range(1024)],
            [float(i + 1) for i in range(1024)],
        ]
        resp = client.post("/embeddings", json={"texts": ["text one", "text two"]})
        assert resp.status_code == 200
        assert len(resp.json()["vectors"]) == 2

    def test_rejects_empty_texts(self, client: TestClient, mock_service: EmbeddingService):
        """Pydantic min_length=1 returns 422."""
        resp = client.post("/embeddings", json={"texts": []})
        assert resp.status_code == 422

    def test_rejects_batch_exceeding_max(self, client: TestClient, mock_service: EmbeddingService):
        """Pydantic max_length=MAX_BATCH returns 422."""
        resp = client.post("/embeddings", json={"texts": ["x"] * (MAX_BATCH + 1)})
        assert resp.status_code == 422

    def test_returns_500_on_provider_error(self, client: TestClient, mock_service: EmbeddingService):
        mock_service._provider.embed.side_effect = RuntimeError("Bedrock timeout")
        resp = client.post("/embeddings", json={"texts": ["hello"]})
        assert resp.status_code == 500
        assert "Bedrock timeout" in resp.json()["error"]

    def test_missing_texts_field_returns_422(self, client: TestClient, mock_service: EmbeddingService):
        resp = client.post("/embeddings", json={})
        assert resp.status_code == 422

    def test_max_batch_size_accepted(self, client: TestClient, mock_service: EmbeddingService):
        """Exactly MAX_BATCH texts should succeed."""
        mock_service._provider.embed.return_value = [
            [0.1] * 1024 for _ in range(MAX_BATCH)
        ]
        resp = client.post("/embeddings", json={"texts": ["t"] * MAX_BATCH})
        assert resp.status_code == 200
        assert len(resp.json()["vectors"]) == MAX_BATCH


# ---------------------------------------------------------------------------
# GET /embeddings/config endpoint
# ---------------------------------------------------------------------------

class TestEmbedConfigEndpoint:
    def test_returns_config_when_service_active(self, client: TestClient, mock_service: EmbeddingService):
        resp = client.get("/embeddings/config")
        assert resp.status_code == 200
        data = resp.json()
        assert data["model"] == "cohere.embed-v4"
        assert data["dim"] == 1024
        assert "provider" in data

    def test_returns_config_from_settings_when_service_not_configured(self, client: TestClient):
        set_embedding_service(None)  # type: ignore[arg-type]
        resp = client.get("/embeddings/config")
        assert resp.status_code == 200
        data = resp.json()
        # Falls back to conductor.settings.yaml defaults
        assert data["model"] == "cohere.embed-english-v3"
        assert data["dim"] == 1024
        assert data["provider"] == "bedrock"

    def test_config_reflects_settings_yaml_override(self, tmp_path):
        """Changing conductor.settings.yaml changes what /embeddings/config returns."""
        from app.config import load_config, _config as orig
        import app.config as cfg_module
        settings = tmp_path / "conductor.settings.yaml"
        settings.write_text(
            "embedding:\n  provider: openai\n  model: text-embedding-3-small\n  dim: 1536\n"
        )
        custom_cfg = load_config(settings_path=settings)
        # Temporarily override the global config singleton
        original = cfg_module._config
        cfg_module._config = custom_cfg
        try:
            set_embedding_service(None)  # type: ignore[arg-type]
            resp = TestClient(app).get("/embeddings/config")
            assert resp.status_code == 200
            data = resp.json()
            assert data["model"] == "text-embedding-3-small"
            assert data["dim"] == 1536
            assert data["provider"] == "openai"
        finally:
            cfg_module._config = original


# ---------------------------------------------------------------------------
# EmbeddingConfig loading (conductor.settings.yaml)
# ---------------------------------------------------------------------------

class TestEmbeddingConfig:
    def test_default_config_values(self):
        from app.config import load_config
        cfg = load_config()
        assert cfg.embedding.provider == "bedrock"
        assert cfg.embedding.model == "cohere.embed-english-v3"
        assert cfg.embedding.dim == 1024

    def test_config_overridable_via_settings(self, tmp_path):
        from app.config import load_config
        settings = tmp_path / "conductor.settings.yaml"
        settings.write_text(
            "embedding:\n  provider: openai\n  model: text-embedding-3-small\n  dim: 1536\n"
        )
        cfg = load_config(settings_path=settings)
        assert cfg.embedding.provider == "openai"
        assert cfg.embedding.model == "text-embedding-3-small"
        assert cfg.embedding.dim == 1536
