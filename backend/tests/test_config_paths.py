"""Tests for config path resolution behavior."""

from pathlib import Path

from app.config import load_config


def test_audit_path_relative_to_project_root_when_settings_in_config_dir(tmp_path):
    """Relative audit_path resolves from project root for ./config layout."""
    project_root = tmp_path / "project"
    config_dir = project_root / "config"
    config_dir.mkdir(parents=True)

    settings_file = config_dir / "conductor.settings.yaml"
    settings_file.write_text(
        "logging:\n  audit_enabled: true\n  audit_path: backend/audit_logs.db\n",
        encoding="utf-8",
    )

    cfg = load_config(settings_path=settings_file)
    assert Path(cfg.logging.audit_path) == project_root / "backend" / "audit_logs.db"


def test_audit_path_relative_to_settings_dir_for_nonstandard_layout(tmp_path):
    """Relative audit_path resolves from settings file directory otherwise."""
    settings_file = tmp_path / "conductor.settings.yaml"
    settings_file.write_text(
        "logging:\n  audit_enabled: true\n  audit_path: local/audit_logs.db\n",
        encoding="utf-8",
    )

    cfg = load_config(settings_path=settings_file)
    assert Path(cfg.logging.audit_path) == tmp_path / "local" / "audit_logs.db"


def test_audit_path_absolute_remains_unchanged(tmp_path):
    """Absolute audit_path is preserved exactly as configured."""
    absolute_path = tmp_path / "absolute" / "audit_logs.db"
    settings_file = tmp_path / "conductor.settings.yaml"
    settings_file.write_text(
        f"logging:\n  audit_enabled: true\n  audit_path: {absolute_path}\n",
        encoding="utf-8",
    )

    cfg = load_config(settings_path=settings_file)
    assert Path(cfg.logging.audit_path) == absolute_path
