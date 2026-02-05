"""Tests for the MockAgent and generate-changes endpoint."""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from jsonschema import validate, ValidationError as JsonSchemaValidationError
from pydantic import ValidationError

from app.main import app
from app.agent.schemas import (
    ChangeSet,
    ChangeType,
    FileChange,
    Range,
    GenerateChangesRequest,
    GenerateChangesResponse,
)
from app.agent.mock_agent import MockAgent


client = TestClient(app)

# Load the JSON schema for validation
SCHEMA_PATH = Path(__file__).parent.parent.parent / "shared" / "changeset.schema.json"


@pytest.fixture
def changeset_schema():
    """Load the ChangeSet JSON schema."""
    with open(SCHEMA_PATH) as f:
        return json.load(f)


class TestChangeSetSchemas:
    """Test Pydantic schema validation for ChangeSet."""

    def test_range_valid(self):
        """Test valid Range creation."""
        r = Range(start=1, end=5)
        assert r.start == 1
        assert r.end == 5

    def test_range_invalid_line_numbers(self):
        """Test Range with invalid line numbers."""
        with pytest.raises(ValidationError):
            Range(start=0, end=5)  # start must be >= 1

        with pytest.raises(ValidationError):
            Range(start=1, end=0)  # end must be >= 1

    def test_file_change_replace_range_valid(self):
        """Test valid FileChange with replace_range type."""
        fc = FileChange(
            file="src/main.py",
            type=ChangeType.REPLACE_RANGE,
            range=Range(start=1, end=3),
            content="new code"
        )
        assert fc.file == "src/main.py"
        assert fc.type == ChangeType.REPLACE_RANGE
        assert fc.original_content is None

    def test_file_change_create_file_valid(self):
        """Test valid FileChange with create_file type."""
        fc = FileChange(
            file="new_file.py",
            type=ChangeType.CREATE_FILE,
            content="# New file content"
        )
        assert fc.file == "new_file.py"
        assert fc.type == ChangeType.CREATE_FILE
        assert fc.range is None

    def test_file_change_replace_range_requires_range(self):
        """Test that replace_range type requires range field."""
        with pytest.raises(ValidationError):
            FileChange(
                file="test.py",
                type=ChangeType.REPLACE_RANGE,
                content="new content"  # missing range
            )

    def test_file_change_replace_range_requires_content(self):
        """Test that replace_range type requires content field."""
        with pytest.raises(ValidationError):
            FileChange(
                file="test.py",
                type=ChangeType.REPLACE_RANGE,
                range=Range(start=1, end=5)  # missing content
            )

    def test_file_change_create_file_requires_content(self):
        """Test that create_file type requires content field."""
        with pytest.raises(ValidationError):
            FileChange(
                file="test.py",
                type=ChangeType.CREATE_FILE
                # missing content
            )

    def test_change_set_valid(self):
        """Test valid ChangeSet creation."""
        cs = ChangeSet(
            changes=[
                FileChange(
                    file="test.py",
                    type=ChangeType.REPLACE_RANGE,
                    range=Range(start=1, end=2),
                    content="x"
                )
            ],
            summary="Test change"
        )
        assert len(cs.changes) == 1
        assert cs.summary == "Test change"

    def test_change_set_multiple_files(self):
        """Test ChangeSet supports multiple files (up to 10)."""
        # Two files should be valid now
        cs = ChangeSet(
            changes=[
                FileChange(
                    file="file1.py",
                    type=ChangeType.REPLACE_RANGE,
                    range=Range(start=1, end=1),
                    content="a"
                ),
                FileChange(
                    file="file2.py",
                    type=ChangeType.REPLACE_RANGE,
                    range=Range(start=1, end=1),
                    content="b"
                )
            ]
        )
        assert len(cs.changes) == 2

    def test_change_set_max_ten_files(self):
        """Test ChangeSet enforces max 10 file constraint."""
        # Create 11 file changes
        changes = [
            FileChange(
                file=f"file{i}.py",
                type=ChangeType.REPLACE_RANGE,
                range=Range(start=1, end=1),
                content="x"
            )
            for i in range(11)
        ]
        with pytest.raises(ValidationError):
            ChangeSet(changes=changes)

    def test_generate_changes_request_valid(self):
        """Test valid GenerateChangesRequest creation."""
        req = GenerateChangesRequest(
            file_path="src/app.py",
            instruction="Add a hello world function"
        )
        assert req.file_path == "src/app.py"
        assert req.instruction == "Add a hello world function"
        assert req.file_content is None

    def test_generate_changes_response_valid(self):
        """Test valid GenerateChangesResponse creation."""
        resp = GenerateChangesResponse(
            success=True,
            change_set=ChangeSet(
                changes=[
                    FileChange(
                        file="test.py",
                        type=ChangeType.REPLACE_RANGE,
                        range=Range(start=1, end=1),
                        content="x"
                    )
                ]
            ),
            message="OK"
        )
        assert resp.success is True
        assert len(resp.change_set.changes) == 1


class TestMockAgent:
    """Test MockAgent functionality."""

    def test_generate_changes_returns_valid_changeset(self):
        """MockAgent should return a valid ChangeSet with 3 changes."""
        agent = MockAgent()
        request = GenerateChangesRequest(
            file_path="src/main.py",
            instruction="Add logging"
        )
        response = agent.generate_changes(request)

        assert response.success is True
        # MockAgent returns 3 changes: 2 create_file + 1 replace_range
        assert len(response.change_set.changes) == 3

        # First change: create helper.py
        assert response.change_set.changes[0].file == "src/helper.py"
        assert response.change_set.changes[0].type == ChangeType.CREATE_FILE

        # Second change: create config.py
        assert response.change_set.changes[1].file == "src/config.py"
        assert response.change_set.changes[1].type == ChangeType.CREATE_FILE

        # Third change: modify original file
        assert response.change_set.changes[2].file == "src/main.py"
        assert response.change_set.changes[2].type == ChangeType.REPLACE_RANGE

        # Verify each change has a unique UUID
        uuids = [change.id for change in response.change_set.changes]
        assert all(len(uid) > 0 for uid in uuids), "All changes should have a UUID"
        assert len(set(uuids)) == len(uuids), "All UUIDs should be unique"

    def test_generate_changes_creates_helper_module(self):
        """MockAgent should create a helper.py with utility functions."""
        agent = MockAgent()
        request = GenerateChangesRequest(
            file_path="app.py",
            instruction="Add helpers"
        )
        response = agent.generate_changes(request)

        helper_change = response.change_set.changes[0]
        assert helper_change.file == "helper.py"
        assert "def format_output" in helper_change.content
        assert "Helper utilities" in helper_change.content

    def test_generate_changes_creates_config_module(self):
        """MockAgent should create a config.py with settings."""
        agent = MockAgent()
        request = GenerateChangesRequest(
            file_path="app.py",
            instruction="Add config"
        )
        response = agent.generate_changes(request)

        config_change = response.change_set.changes[1]
        assert config_change.file == "config.py"
        assert "DEBUG" in config_change.content
        assert "VERSION" in config_change.content

    def test_generate_changes_modifies_original_file(self):
        """MockAgent should add imports to the original file."""
        agent = MockAgent()
        original = "# Original file content\nprint('hello')"
        request = GenerateChangesRequest(
            file_path="test.py",
            instruction="Modify content",
            file_content=original
        )
        response = agent.generate_changes(request)

        # Third change modifies the original file
        modify_change = response.change_set.changes[2]
        assert modify_change.file == "test.py"
        assert modify_change.type == ChangeType.REPLACE_RANGE
        assert "from helper import" in modify_change.content
        assert "from config import" in modify_change.content
        # Original content should be the first line
        assert modify_change.original_content == "# Original file content"

    def test_generate_changes_without_file_path(self):
        """MockAgent should only create files when no file_path is provided."""
        agent = MockAgent()
        request = GenerateChangesRequest(
            instruction="Create new modules"
            # file_path is None
        )
        response = agent.generate_changes(request)

        assert response.success is True
        # Only 2 create_file operations (no replace_range)
        assert len(response.change_set.changes) == 2

        # First change: create helper.py in workspace root
        assert response.change_set.changes[0].file == "helper.py"
        assert response.change_set.changes[0].type == ChangeType.CREATE_FILE

        # Second change: create config.py in workspace root
        assert response.change_set.changes[1].file == "config.py"
        assert response.change_set.changes[1].type == ChangeType.CREATE_FILE

        # Summary should not mention updating a file
        assert "update" not in response.change_set.summary.lower()


class TestGenerateChangesEndpoint:
    """Test the /generate-changes endpoint."""

    def test_endpoint_returns_valid_json(self):
        """Endpoint should return valid JSON matching the schema."""
        response = client.post(
            "/generate-changes",
            json={
                "file_path": "src/app.py",
                "instruction": "Add error handling"
            }
        )
        assert response.status_code == 200

        data = response.json()
        assert "success" in data
        assert "change_set" in data
        assert "message" in data

    def test_endpoint_changeset_structure(self):
        """Endpoint should return properly structured ChangeSet."""
        response = client.post(
            "/generate-changes",
            json={
                "file_path": "main.py",
                "instruction": "Refactor function"
            }
        )
        data = response.json()

        # Validate ChangeSet structure
        change_set = data["change_set"]
        assert "changes" in change_set
        assert "summary" in change_set
        # MockAgent returns 3 changes: 2 create_file + 1 replace_range
        assert len(change_set["changes"]) == 3

        # First change: create helper.py
        helper_change = change_set["changes"][0]
        assert helper_change["file"] == "helper.py"
        assert helper_change["type"] == "create_file"
        assert "content" in helper_change

        # Second change: create config.py
        config_change = change_set["changes"][1]
        assert config_change["file"] == "config.py"
        assert config_change["type"] == "create_file"

        # Third change: modify main.py
        modify_change = change_set["changes"][2]
        assert modify_change["file"] == "main.py"
        assert modify_change["type"] == "replace_range"
        assert "range" in modify_change
        assert modify_change["range"]["start"] >= 1

    def test_endpoint_invalid_request(self):
        """Endpoint should return 422 for invalid requests."""
        # Missing required 'instruction' field
        response = client.post(
            "/generate-changes",
            json={"file_path": "test.py"}
        )
        assert response.status_code == 422

    def test_endpoint_response_validates_as_pydantic_model(self):
        """Response should be parseable as GenerateChangesResponse."""
        response = client.post(
            "/generate-changes",
            json={
                "file_path": "test.py",
                "instruction": "Add tests"
            }
        )
        data = response.json()

        # This should not raise ValidationError
        parsed = GenerateChangesResponse(**data)
        assert parsed.success is True
        # MockAgent now returns 3 files for testing sequential review
        assert len(parsed.change_set.changes) == 3



class TestJsonSchemaValidation:
    """Test that MockAgent output validates against the JSON schema."""

    def test_mock_agent_output_validates_against_schema(self, changeset_schema):
        """MockAgent ChangeSet should validate against the JSON schema."""
        agent = MockAgent()
        request = GenerateChangesRequest(
            file_path="src/main.py",
            instruction="Add logging"
        )
        response = agent.generate_changes(request)

        # Convert Pydantic model to dict for JSON schema validation
        change_set_dict = response.change_set.model_dump()

        # This should not raise ValidationError
        validate(instance=change_set_dict, schema=changeset_schema)

    def test_endpoint_output_validates_against_schema(self, changeset_schema):
        """Endpoint ChangeSet should validate against the JSON schema."""
        response = client.post(
            "/generate-changes",
            json={
                "file_path": "test.py",
                "instruction": "Add tests"
            }
        )
        data = response.json()

        # Extract the change_set and validate
        change_set = data["change_set"]
        validate(instance=change_set, schema=changeset_schema)

    def test_schema_rejects_invalid_type(self, changeset_schema):
        """Schema should reject invalid change types."""
        invalid_data = {
            "changes": [{
                "id": "test-uuid-12345",
                "file": "test.py",
                "type": "invalid_type",  # Invalid type
                "content": "test"
            }],
            "summary": "Test"
        }

        with pytest.raises(JsonSchemaValidationError):
            validate(instance=invalid_data, schema=changeset_schema)

    def test_schema_requires_range_for_replace_range(self, changeset_schema):
        """Schema should require range field for replace_range type."""
        invalid_data = {
            "changes": [{
                "id": "test-uuid-12345",
                "file": "test.py",
                "type": "replace_range",
                "content": "test"  # missing range
            }],
            "summary": "Test"
        }

        with pytest.raises(JsonSchemaValidationError):
            validate(instance=invalid_data, schema=changeset_schema)

    def test_schema_accepts_create_file_without_range(self, changeset_schema):
        """Schema should accept create_file without range field."""
        valid_data = {
            "changes": [{
                "id": "test-uuid-12345",
                "file": "new_file.py",
                "type": "create_file",
                "content": "# New file content"
            }],
            "summary": "Create new file"
        }

        # This should not raise ValidationError
        validate(instance=valid_data, schema=changeset_schema)
