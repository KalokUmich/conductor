"""Tests for Auto Apply policy evaluation."""
import pytest

from app.agent.schemas import ChangeSet, ChangeType, FileChange, Range
from app.policy.auto_apply import (
    AutoApplyPolicy,
    PolicyResult,
    evaluate_auto_apply,
    MAX_FILES,
    MAX_LINES_CHANGED,
    FORBIDDEN_PATHS,
)


class TestPolicyResult:
    """Test PolicyResult dataclass."""

    def test_allowed_result_is_truthy(self):
        """PolicyResult with allowed=True should be truthy."""
        result = PolicyResult(allowed=True)
        assert result
        assert result.allowed is True
        assert result.reasons == []

    def test_denied_result_is_falsy(self):
        """PolicyResult with allowed=False should be falsy."""
        result = PolicyResult(allowed=False, reasons=["test reason"])
        assert not result
        assert result.allowed is False
        assert result.reasons == ["test reason"]


class TestAutoApplyPolicyMaxFiles:
    """Test max_files rule."""

    def test_single_file_passes(self):
        """Single file change should pass."""
        change_set = ChangeSet(
            changes=[
                FileChange(
                    file="src/main.py",
                    type=ChangeType.REPLACE_RANGE,
                    range=Range(start=1, end=5),
                    content="# new content"
                )
            ]
        )
        result = evaluate_auto_apply(change_set)
        assert result.allowed is True

    def test_two_files_passes(self):
        """Two file changes should pass (max_files=2)."""
        policy = AutoApplyPolicy(max_files=2)
        # Note: Current ChangeSet schema limits to 1 file, so we test with custom policy
        change_set = ChangeSet(
            changes=[
                FileChange(
                    file="src/main.py",
                    type=ChangeType.REPLACE_RANGE,
                    range=Range(start=1, end=5),
                    content="# new content"
                )
            ]
        )
        result = policy.evaluate(change_set)
        assert result.allowed is True

    def test_exceeding_max_files_fails(self):
        """Exceeding max_files should fail."""
        # Create a policy with max_files=0 to test failure
        policy = AutoApplyPolicy(max_files=0)
        change_set = ChangeSet(
            changes=[
                FileChange(
                    file="src/main.py",
                    type=ChangeType.REPLACE_RANGE,
                    range=Range(start=1, end=5),
                    content="# new content"
                )
            ]
        )
        result = policy.evaluate(change_set)
        assert result.allowed is False
        assert any("Too many files" in r for r in result.reasons)


class TestAutoApplyPolicyMaxLines:
    """Test max_lines_changed rule."""

    def test_small_change_passes(self):
        """Small change (5 lines) should pass."""
        change_set = ChangeSet(
            changes=[
                FileChange(
                    file="src/main.py",
                    type=ChangeType.REPLACE_RANGE,
                    range=Range(start=1, end=5),
                    content="# new content"
                )
            ]
        )
        result = evaluate_auto_apply(change_set)
        assert result.allowed is True

    def test_exactly_max_lines_passes(self):
        """Exactly max_lines_changed should pass."""
        policy = AutoApplyPolicy(max_lines_changed=50)
        change_set = ChangeSet(
            changes=[
                FileChange(
                    file="src/main.py",
                    type=ChangeType.REPLACE_RANGE,
                    range=Range(start=1, end=50),  # 50 lines
                    content="# new content"
                )
            ]
        )
        result = policy.evaluate(change_set)
        assert result.allowed is True

    def test_exceeding_max_lines_fails(self):
        """Exceeding max_lines_changed should fail."""
        policy = AutoApplyPolicy(max_lines_changed=10)
        change_set = ChangeSet(
            changes=[
                FileChange(
                    file="src/main.py",
                    type=ChangeType.REPLACE_RANGE,
                    range=Range(start=1, end=20),  # 20 lines > 10
                    content="# new content"
                )
            ]
        )
        result = policy.evaluate(change_set)
        assert result.allowed is False
        assert any("Too many lines" in r for r in result.reasons)

    def test_create_file_counts_content_lines(self):
        """Create file should count lines in content."""
        policy = AutoApplyPolicy(max_lines_changed=5)
        # Content with 10 lines (9 newlines + 1)
        content = "\n".join([f"line {i}" for i in range(10)])
        change_set = ChangeSet(
            changes=[
                FileChange(
                    file="new_file.py",
                    type=ChangeType.CREATE_FILE,
                    content=content
                )
            ]
        )
        result = policy.evaluate(change_set)
        assert result.allowed is False
        assert any("Too many lines" in r for r in result.reasons)


class TestAutoApplyPolicyForbiddenPaths:
    """Test forbidden paths rule."""

    @pytest.mark.parametrize("forbidden_path", [
        "infra/terraform/main.tf",
        "infra/config.yaml",
        "db/migrations/001.sql",
        "db/schema.py",
        "security/auth.py",
        "security/keys/private.pem",
    ])
    def test_forbidden_paths_fail(self, forbidden_path: str):
        """Files in forbidden paths should fail."""
        change_set = ChangeSet(
            changes=[
                FileChange(
                    file=forbidden_path,
                    type=ChangeType.REPLACE_RANGE,
                    range=Range(start=1, end=5),
                    content="# new content"
                )
            ]
        )
        result = evaluate_auto_apply(change_set)
        assert result.allowed is False
        assert any("Forbidden paths" in r for r in result.reasons)

    @pytest.mark.parametrize("allowed_path", [
        "src/main.py",
        "tests/test_main.py",
        "lib/utils.py",
        "infrastructure/setup.py",  # Not "infra/"
        "database/models.py",  # Not "db/"
        "secure/handler.py",  # Not "security/"
    ])
    def test_allowed_paths_pass(self, allowed_path: str):
        """Files not in forbidden paths should pass."""
        change_set = ChangeSet(
            changes=[
                FileChange(
                    file=allowed_path,
                    type=ChangeType.REPLACE_RANGE,
                    range=Range(start=1, end=5),
                    content="# new content"
                )
            ]
        )
        result = evaluate_auto_apply(change_set)
        assert result.allowed is True


class TestAutoApplyPolicyMultipleViolations:
    """Test multiple rule violations."""

    def test_multiple_violations_reported(self):
        """All violations should be reported in reasons."""
        policy = AutoApplyPolicy(
            max_files=0,  # Will fail
            max_lines_changed=1,  # Will fail (5 lines)
            forbidden_paths=("src/",)  # Will fail
        )
        change_set = ChangeSet(
            changes=[
                FileChange(
                    file="src/main.py",
                    type=ChangeType.REPLACE_RANGE,
                    range=Range(start=1, end=5),
                    content="# new content"
                )
            ]
        )
        result = policy.evaluate(change_set)
        assert result.allowed is False
        assert len(result.reasons) == 3
        assert any("Too many files" in r for r in result.reasons)
        assert any("Too many lines" in r for r in result.reasons)
        assert any("Forbidden paths" in r for r in result.reasons)


class TestAutoApplyPolicyDefaults:
    """Test default policy constants."""

    def test_default_max_files(self):
        """Default max_files should be 2."""
        assert MAX_FILES == 2

    def test_default_max_lines_changed(self):
        """Default max_lines_changed should be 50."""
        assert MAX_LINES_CHANGED == 50

    def test_default_forbidden_paths(self):
        """Default forbidden paths should include infra/, db/, security/."""
        assert "infra/" in FORBIDDEN_PATHS
        assert "db/" in FORBIDDEN_PATHS
        assert "security/" in FORBIDDEN_PATHS


class TestPolicyRouter:
    """Test the policy router endpoint."""

    def test_evaluate_auto_apply_endpoint_allowed(self):
        """Test /policy/evaluate-auto-apply returns allowed=True for safe changes."""
        from fastapi.testclient import TestClient
        from app.main import app

        client = TestClient(app)
        response = client.post(
            "/policy/evaluate-auto-apply",
            json={
                "change_set": {
                    "changes": [
                        {
                            "file": "src/main.py",
                            "type": "replace_range",
                            "range": {"start": 1, "end": 5},
                            "content": "# new content"
                        }
                    ]
                }
            }
        )
        assert response.status_code == 200
        data = response.json()
        assert data["allowed"] is True
        assert data["reasons"] == []
        assert data["files_count"] == 1
        assert "lines_changed" in data

    def test_evaluate_auto_apply_endpoint_denied_too_many_lines(self):
        """Test /policy/evaluate-auto-apply returns allowed=False for too many lines."""
        from fastapi.testclient import TestClient
        from app.main import app

        client = TestClient(app)
        # Create a replace_range change with >50 lines in the range
        # The policy counts lines in the range (end - start + 1)
        response = client.post(
            "/policy/evaluate-auto-apply",
            json={
                "change_set": {
                    "changes": [
                        {"file": "src/main.py", "type": "replace_range", "range": {"start": 1, "end": 60}, "content": "# new content"},
                    ]
                }
            }
        )
        assert response.status_code == 200
        data = response.json()
        assert data["allowed"] is False
        assert any("lines" in r.lower() for r in data["reasons"])
        assert data["lines_changed"] > 50

    def test_evaluate_auto_apply_endpoint_denied_forbidden_path(self):
        """Test /policy/evaluate-auto-apply returns allowed=False for forbidden paths."""
        from fastapi.testclient import TestClient
        from app.main import app

        client = TestClient(app)
        response = client.post(
            "/policy/evaluate-auto-apply",
            json={
                "change_set": {
                    "changes": [
                        {
                            "file": "security/auth.py",
                            "type": "replace_range",
                            "range": {"start": 1, "end": 1},
                            "content": "# hacked"
                        }
                    ]
                }
            }
        )
        assert response.status_code == 200
        data = response.json()
        assert data["allowed"] is False
        assert any("forbidden" in r.lower() for r in data["reasons"])
