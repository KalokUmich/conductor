"""Diff ingestion layer — parses git diff into structured PRContext.

Uses the existing git_diff_files tool to get the file list, then
classifies each file by category and computes totals.
"""
from __future__ import annotations

import logging
import re
from typing import List

from app.code_tools.tools import git_diff_files

from .models import ChangedFile, FileCategory, PRContext

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# File classification rules
# ---------------------------------------------------------------------------

_TEST_PATTERNS = [
    re.compile(r"(?:^|/)tests?/", re.IGNORECASE),
    re.compile(r"(?:^|/)__tests__/", re.IGNORECASE),
    re.compile(r"(?:^|/)spec/", re.IGNORECASE),
    re.compile(r"test_\w+\.py$", re.IGNORECASE),
    re.compile(r"\w+Test\.java$"),
    re.compile(r"\w+\.test\.\w+$"),
    re.compile(r"\w+\.spec\.\w+$"),
    re.compile(r"_test\.go$"),
]

_CONFIG_PATTERNS = [
    re.compile(r"\.ya?ml$"),
    re.compile(r"\.properties$"),
    re.compile(r"\.env(\.\w+)?$"),
    re.compile(r"\.toml$"),
    re.compile(r"\.ini$"),
    re.compile(r"\.cfg$"),
    re.compile(r"(?:^|/)config/"),
    re.compile(r"settings\.\w+$"),
]

_INFRA_PATTERNS = [
    re.compile(r"(?:^|/)\.github/"),
    re.compile(r"(?:^|/)\.gitlab-ci"),
    re.compile(r"Dockerfile"),
    re.compile(r"docker-compose"),
    re.compile(r"(?:^|/)terraform/"),
    re.compile(r"(?:^|/)infra/"),
    re.compile(r"Makefile$"),
    re.compile(r"Jenkinsfile$"),
    re.compile(r"(?:^|/)\.circleci/"),
]

_SCHEMA_PATTERNS = [
    re.compile(r"(?:^|/)migrations?/"),
    re.compile(r"(?:^|/)alembic/"),
    re.compile(r"(?:^|/)flyway/"),
    re.compile(r"(?:^|/)liquibase/"),
    re.compile(r"\.sql$"),
    re.compile(r"schema\.\w+$"),
]

_GENERATED_PATTERNS = [
    re.compile(r"\.lock$"),
    re.compile(r"package-lock\.json$"),
    re.compile(r"yarn\.lock$"),
    re.compile(r"pnpm-lock\.yaml$"),
    re.compile(r"Cargo\.lock$"),
    re.compile(r"go\.sum$"),
    re.compile(r"(?:^|/)vendor/"),
    re.compile(r"(?:^|/)node_modules/"),
    re.compile(r"\.min\.\w+$"),
    re.compile(r"\.generated\.\w+$"),
    re.compile(r"_pb2\.py$"),
    re.compile(r"\.pb\.go$"),
    re.compile(r"(?:^|/)dist/"),
    re.compile(r"(?:^|/)build/"),
    re.compile(r"__pycache__/"),
]


def _classify_file(path: str) -> FileCategory:
    """Classify a file path into a review category."""
    for pattern in _GENERATED_PATTERNS:
        if pattern.search(path):
            return FileCategory.GENERATED
    for pattern in _TEST_PATTERNS:
        if pattern.search(path):
            return FileCategory.TEST
    for pattern in _SCHEMA_PATTERNS:
        if pattern.search(path):
            return FileCategory.SCHEMA
    for pattern in _INFRA_PATTERNS:
        if pattern.search(path):
            return FileCategory.INFRA
    for pattern in _CONFIG_PATTERNS:
        if pattern.search(path):
            return FileCategory.CONFIG
    return FileCategory.BUSINESS_LOGIC


def parse_diff(workspace_path: str, diff_spec: str) -> PRContext:
    """Parse a git diff spec into a structured PRContext.

    Uses the existing ``git_diff_files`` code tool to get the file list,
    then classifies each file and computes totals.

    Args:
        workspace_path: Absolute path to the git workspace.
        diff_spec: Git ref spec, e.g. "main...feature/branch" or "HEAD~5".

    Returns:
        PRContext with classified files and aggregate stats.
    """
    result = git_diff_files(workspace=workspace_path, ref=diff_spec)

    if not result.success:
        logger.warning("git_diff_files failed: %s", result.error)
        return PRContext(diff_spec=diff_spec)

    files: List[ChangedFile] = []
    total_add = 0
    total_del = 0

    for entry in (result.data or []):
        additions = entry.get("additions", 0)
        deletions = entry.get("deletions", 0)
        path = entry.get("path", "")

        cf = ChangedFile(
            path=path,
            status=entry.get("status", "modified"),
            additions=additions,
            deletions=deletions,
            category=_classify_file(path),
            old_path=entry.get("old_path"),
        )
        files.append(cf)
        total_add += additions
        total_del += deletions

    return PRContext(
        diff_spec=diff_spec,
        files=files,
        total_additions=total_add,
        total_deletions=total_del,
        total_changed_lines=total_add + total_del,
        file_count=len(files),
    )
