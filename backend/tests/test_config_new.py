"""Tests for the configuration module.

Covers:
* _find_config_file — search path logic
* AppSettings instantiation with all fields
* CodeSearchSettings — repo map configuration
* Secrets — database, JWT
* load_settings — YAML loading + merging via _find_config_file
* Edge cases: missing files, empty values
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock, patch

import yaml

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

from app.config import (
    AppSettings,
    CodeSearchSettings,
    DatabaseSecrets,
    JWTSecrets,
    Secrets,
    _find_config_file,
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
        target.write_text("jwt: {}")
        assert _find_config_file("conductor.secrets.yaml") == target


# ===================================================================
# CodeSearchSettings
# ===================================================================


class TestCodeSearchSettingsDefaults:
    def test_default_repo_map_enabled(self):
        cfg = CodeSearchSettings()
        assert cfg.repo_map_enabled is True

    def test_default_repo_map_top_n(self):
        cfg = CodeSearchSettings()
        assert cfg.repo_map_top_n == 10


class TestCodeSearchSettingsCustom:
    def test_custom_repo_map_top_n(self):
        cfg = CodeSearchSettings(repo_map_top_n=20)
        assert cfg.repo_map_top_n == 20

    def test_disable_repo_map(self):
        cfg = CodeSearchSettings(repo_map_enabled=False)
        assert cfg.repo_map_enabled is False


class TestCodeSearchSettingsSerialization:
    def test_model_dump(self):
        cfg = CodeSearchSettings()
        d = cfg.model_dump()
        assert isinstance(d, dict)
        assert "repo_map_enabled" in d


# ===================================================================
# Secrets models
# ===================================================================


class TestSecrets:
    def test_has_database(self):
        s = Secrets()
        assert isinstance(s.database, DatabaseSecrets)

    def test_has_jwt(self):
        s = Secrets()
        assert isinstance(s.jwt, JWTSecrets)

    def test_jwt_defaults(self):
        s = Secrets()
        assert s.jwt.secret_key == "change-me-in-production"
        assert s.jwt.algorithm == "HS256"


# ===================================================================
# AppSettings
# ===================================================================


class TestAppSettings:
    def test_default_instantiation(self):
        cfg = AppSettings()
        assert cfg.code_search.repo_map_enabled is True

    def test_has_all_sections(self):
        cfg = AppSettings()
        assert cfg.server is not None
        assert cfg.database is not None
        assert cfg.auth is not None
        assert cfg.rooms is not None
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

        with patch("app.config._find_config_file", side_effect=mock_find):
            settings = load_settings()
            assert isinstance(settings, AppSettings)

    def test_loads_from_yaml(self, tmp_path):
        settings_file = tmp_path / "conductor.settings.yaml"
        settings_file.write_text(
            yaml.dump(
                {
                    "code_search": {
                        "repo_map_top_n": 15,
                    }
                }
            )
        )
        secrets_file = tmp_path / "conductor.secrets.yaml"
        secrets_file.write_text(yaml.dump({"jwt": {"secret_key": "test-key"}}))

        def mock_find(filename):
            if "settings" in filename:
                return settings_file
            return secrets_file

        with patch("app.config._find_config_file", side_effect=mock_find):
            settings = load_settings()
            assert settings.code_search.repo_map_top_n == 15
            assert settings.secrets.jwt.secret_key == "test-key"
