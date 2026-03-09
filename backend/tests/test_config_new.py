"""Tests for the updated configuration module.

Covers:
* _find_config_file — search path logic
* AppSettings instantiation with all new fields
* CodeSearchSettings — all 5 backends, model defaults, validation
* VoyageSecrets + MistralSecrets + CohereSecrets
* _inject_embedding_env_vars — all 5 embedding + reranking backends
* load_settings — YAML loading + merging via _find_config_file
* Edge cases: missing files, empty values, unknown backends
"""
from __future__ import annotations

import os
import sys
import types
import pytest
import yaml
from pathlib import Path
from unittest.mock import patch, MagicMock

# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------

def _stub(name: str, **attrs) -> types.ModuleType:
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules.setdefault(name, m)
    return m

_stub("cocoindex")
_stub("sentence_transformers", SentenceTransformer=MagicMock)
_stub("sqlite_vec")

# ---------------------------------------------------------------------------
# Real imports
# ---------------------------------------------------------------------------

from backend.app.config import (  # noqa: E402
    AppSettings,
    CodeSearchSettings,
    GitWorkspaceSettings,
    ServerSettings,
    Secrets,
    AwsSecrets,
    OpenAISecrets,
    VoyageSecrets,
    MistralSecrets,
    _find_config_file,
    _inject_embedding_env_vars,
    load_settings,
)


# ===================================================================
# _find_config_file
# ===================================================================


class TestFindConfigFile:
    def test_finds_in_config_subdir(self, tmp_path, monkeypatch):
        """Priority 1: ./config/{filename}"""
        monkeypatch.chdir(tmp_path)
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        target = config_dir / "conductor.settings.yaml"
        target.write_text("server: {}")
        assert _find_config_file("conductor.settings.yaml") == target

    def test_finds_in_cwd(self, tmp_path, monkeypatch):
        """Priority 2: ./{filename} (when config/ doesn't have it)"""
        monkeypatch.chdir(tmp_path)
        target = tmp_path / "conductor.settings.yaml"
        target.write_text("server: {}")
        assert _find_config_file("conductor.settings.yaml") == target

    def test_prefers_config_subdir_over_cwd(self, tmp_path, monkeypatch):
        """config/ should take priority over CWD."""
        monkeypatch.chdir(tmp_path)
        cwd_file = tmp_path / "conductor.settings.yaml"
        cwd_file.write_text("cwd")
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        config_file = config_dir / "conductor.settings.yaml"
        config_file.write_text("config")
        assert _find_config_file("conductor.settings.yaml") == config_file

    def test_finds_in_parent_config(self, tmp_path, monkeypatch):
        """Priority 3: ../config/{filename}"""
        parent = tmp_path / "parent"
        child = parent / "child"
        child.mkdir(parents=True)
        config_dir = parent / "config"
        config_dir.mkdir()
        target = config_dir / "conductor.settings.yaml"
        target.write_text("parent")
        monkeypatch.chdir(child)
        assert _find_config_file("conductor.settings.yaml") == target

    def test_returns_none_when_not_found(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert _find_config_file("conductor.settings.yaml") is None

    def test_finds_secrets_file(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        target = config_dir / "conductor.secrets.yaml"
        target.write_text("aws: {}")
        assert _find_config_file("conductor.secrets.yaml") == target


# ===================================================================
# CodeSearchSettings
# ===================================================================


class TestCodeSearchSettingsDefaults:
    def test_default_backend_is_bedrock(self):
        cfg = CodeSearchSettings()
        assert cfg.embedding_backend == "bedrock"

    def test_default_bedrock_model(self):
        cfg = CodeSearchSettings()
        assert cfg.bedrock_model_id == "cohere.embed-v4:0"

    def test_default_bedrock_region(self):
        cfg = CodeSearchSettings()
        assert cfg.bedrock_region == "us-east-1"

    def test_default_local_model(self):
        cfg = CodeSearchSettings()
        assert cfg.local_model_name == "all-MiniLM-L6-v2"

    def test_default_openai_model(self):
        cfg = CodeSearchSettings()
        assert cfg.openai_model_name == "text-embedding-3-small"

    def test_default_voyage_model(self):
        cfg = CodeSearchSettings()
        assert cfg.voyage_model_name == "voyage-code-3"

    def test_default_mistral_model(self):
        cfg = CodeSearchSettings()
        assert cfg.mistral_model_name == "codestral-embed-2505"

    def test_default_chunk_size(self):
        cfg = CodeSearchSettings()
        assert cfg.chunk_size == 512

    def test_default_top_k(self):
        cfg = CodeSearchSettings()
        assert cfg.top_k_results == 5

    def test_default_repo_map_enabled(self):
        cfg = CodeSearchSettings()
        assert cfg.repo_map_enabled is True

    def test_default_repo_map_top_n(self):
        cfg = CodeSearchSettings()
        assert cfg.repo_map_top_n == 10


class TestCodeSearchSettingsCustom:
    def test_set_local_backend(self):
        cfg = CodeSearchSettings(embedding_backend="local")
        assert cfg.embedding_backend == "local"

    def test_set_openai_backend(self):
        cfg = CodeSearchSettings(embedding_backend="openai")
        assert cfg.embedding_backend == "openai"

    def test_set_voyage_backend(self):
        cfg = CodeSearchSettings(embedding_backend="voyage")
        assert cfg.embedding_backend == "voyage"

    def test_set_mistral_backend(self):
        cfg = CodeSearchSettings(embedding_backend="mistral")
        assert cfg.embedding_backend == "mistral"

    def test_invalid_backend_raises(self):
        with pytest.raises(Exception):  # pydantic ValidationError
            CodeSearchSettings(embedding_backend="invalid")

    def test_custom_bedrock_model(self):
        cfg = CodeSearchSettings(bedrock_model_id="amazon.titan-embed-text-v2:0")
        assert cfg.bedrock_model_id == "amazon.titan-embed-text-v2:0"

    def test_custom_region(self):
        cfg = CodeSearchSettings(bedrock_region="eu-west-1")
        assert cfg.bedrock_region == "eu-west-1"

    def test_custom_chunk_size(self):
        cfg = CodeSearchSettings(chunk_size=1024)
        assert cfg.chunk_size == 1024


class TestCodeSearchSettingsSerialization:
    def test_model_dump(self):
        cfg = CodeSearchSettings()
        d = cfg.model_dump()
        assert isinstance(d, dict)
        assert "embedding_backend" in d
        assert "bedrock_model_id" in d
        assert "voyage_model_name" in d
        assert "mistral_model_name" in d
        assert "repo_map_enabled" in d


# ===================================================================
# Secrets models
# ===================================================================


class TestVoyageSecrets:
    def test_default(self):
        s = VoyageSecrets()
        assert s.api_key is None

    def test_with_key(self):
        s = VoyageSecrets(api_key="pa-test")
        assert s.api_key == "pa-test"


class TestMistralSecrets:
    def test_default(self):
        s = MistralSecrets()
        assert s.api_key is None

    def test_with_key(self):
        s = MistralSecrets(api_key="m-test")
        assert s.api_key == "m-test"


class TestSecrets:
    def test_has_voyage(self):
        s = Secrets()
        assert isinstance(s.voyage, VoyageSecrets)

    def test_has_mistral(self):
        s = Secrets()
        assert isinstance(s.mistral, MistralSecrets)

    def test_all_fields(self):
        s = Secrets(
            aws=AwsSecrets(access_key_id="AK"),
            openai=OpenAISecrets(api_key="sk-test"),
            voyage=VoyageSecrets(api_key="pa-test"),
            mistral=MistralSecrets(api_key="m-test"),
        )
        assert s.aws.access_key_id == "AK"
        assert s.openai.api_key == "sk-test"
        assert s.voyage.api_key == "pa-test"
        assert s.mistral.api_key == "m-test"


# ===================================================================
# _inject_embedding_env_vars
# ===================================================================


class TestInjectEnvVars:
    @pytest.fixture(autouse=True)
    def clean_env(self):
        """Remove test env vars before/after each test."""
        keys = [
            "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_DEFAULT_REGION",
            "OPENAI_API_KEY", "VOYAGE_API_KEY", "MISTRAL_API_KEY",
        ]
        old = {k: os.environ.pop(k, None) for k in keys}
        yield
        for k, v in old.items():
            if v is not None:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)

    def _make_settings(self, backend="local", **secrets_kwargs):
        secrets = Secrets(**secrets_kwargs)
        cs = CodeSearchSettings(embedding_backend=backend)
        return AppSettings(code_search=cs, secrets=secrets)

    def test_bedrock_injects_aws(self):
        settings = self._make_settings(
            backend="bedrock",
            aws=AwsSecrets(access_key_id="AK", secret_access_key="SK", region="us-west-2"),
        )
        _inject_embedding_env_vars(settings)
        assert os.environ["AWS_ACCESS_KEY_ID"] == "AK"
        assert os.environ["AWS_SECRET_ACCESS_KEY"] == "SK"
        assert os.environ["AWS_DEFAULT_REGION"] == "us-west-2"

    def test_openai_injects_key(self):
        settings = self._make_settings(
            backend="openai",
            openai=OpenAISecrets(api_key="sk-hello"),
        )
        _inject_embedding_env_vars(settings)
        assert os.environ["OPENAI_API_KEY"] == "sk-hello"

    def test_voyage_injects_key(self):
        settings = self._make_settings(
            backend="voyage",
            voyage=VoyageSecrets(api_key="pa-hello"),
        )
        _inject_embedding_env_vars(settings)
        assert os.environ["VOYAGE_API_KEY"] == "pa-hello"

    def test_mistral_injects_key(self):
        settings = self._make_settings(
            backend="mistral",
            mistral=MistralSecrets(api_key="m-hello"),
        )
        _inject_embedding_env_vars(settings)
        assert os.environ["MISTRAL_API_KEY"] == "m-hello"

    def test_local_no_injection(self):
        settings = self._make_settings(backend="local")
        _inject_embedding_env_vars(settings)
        assert "AWS_ACCESS_KEY_ID" not in os.environ
        assert "OPENAI_API_KEY" not in os.environ
        assert "VOYAGE_API_KEY" not in os.environ
        assert "MISTRAL_API_KEY" not in os.environ

    def test_bedrock_missing_credentials_no_error(self):
        settings = self._make_settings(backend="bedrock")
        # Should not raise even without credentials
        _inject_embedding_env_vars(settings)


# ===================================================================
# AppSettings
# ===================================================================


class TestAppSettings:
    def test_default_instantiation(self):
        cfg = AppSettings()
        assert cfg.code_search.embedding_backend == "bedrock"

    def test_has_all_sections(self):
        cfg = AppSettings()
        assert cfg.server is not None
        assert cfg.database is not None
        assert cfg.auth is not None
        assert cfg.rooms is not None
        assert cfg.live_share is not None
        assert cfg.git_workspace is not None
        assert cfg.code_search is not None
        assert cfg.secrets is not None

    def test_serialization(self):
        cfg = AppSettings()
        d = cfg.model_dump()
        assert isinstance(d, dict)
        assert "code_search" in d
        assert "secrets" in d


# ===================================================================
# load_settings
# ===================================================================


class TestLoadSettings:
    def test_missing_files_returns_defaults(self, tmp_path):
        def mock_find(filename):
            return tmp_path / filename  # Points to non-existent files

        with patch("backend.app.config._find_config_file", side_effect=mock_find):
            settings = load_settings()
            assert isinstance(settings, AppSettings)

    def test_loads_from_yaml(self, tmp_path):
        settings_file = tmp_path / "conductor.settings.yaml"
        settings_file.write_text(yaml.dump({
            "code_search": {
                "embedding_backend": "local",
                "local_model_name": "custom-model",
            }
        }))
        secrets_file = tmp_path / "conductor.secrets.yaml"
        secrets_file.write_text(yaml.dump({"openai": {"api_key": "sk-test"}}))

        def mock_find(filename):
            if "settings" in filename:
                return settings_file
            return secrets_file

        with patch("backend.app.config._find_config_file", side_effect=mock_find):
            settings = load_settings()
            assert settings.code_search.embedding_backend == "local"
            assert settings.code_search.local_model_name == "custom-model"
            assert settings.secrets.openai.api_key == "sk-test"
