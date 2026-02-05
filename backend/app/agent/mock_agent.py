"""Mock agent for testing code generation without LLM calls.

This module provides a MockAgent that generates deterministic ChangeSets
for testing and development. It simulates the behavior of a real LLM-based
agent by creating predictable file changes.

The MockAgent always generates:
    1. helper.py - A utility module with a format_output function
    2. config.py - A configuration module with DEBUG and VERSION constants
    3. Import statements added to the target file (if provided)

This predictable behavior allows testing of:
    - Diff preview display
    - Sequential change review flow
    - Apply/discard functionality
    - Policy evaluation

Future LLM Integration:
    Replace MockAgent with an LLM-based agent that:
    - Uses code style guidelines from StyleLoader
    - Generates contextual changes based on instructions
    - Respects change limits from configuration
"""
import os
from typing import List, Optional

from .schemas import (
    ChangeSet,
    ChangeType,
    FileChange,
    GenerateChangesRequest,
    GenerateChangesResponse,
    Range,
)


class MockAgent:
    """Mock agent that generates deterministic ChangeSets for testing.

    This agent does not make any LLM API calls. Instead, it generates
    a fixed set of changes that are useful for testing the extension's
    change review and application flow.

    Attributes:
        MAX_LINES_CHANGED: Maximum lines changed per file (for testing).
    """

    MAX_LINES_CHANGED = 5

    def __init__(self) -> None:
        """Initialize the mock agent."""
        pass

    def generate_changes(self, request: GenerateChangesRequest) -> GenerateChangesResponse:
        """
        Generate a mock ChangeSet based on the request.

        If file_path is provided:
        - Creates helper.py and config.py in the same directory
        - Modifies the original file to add imports

        If file_path is not provided:
        - Creates helper.py and config.py in the workspace root

        Args:
            request: The generate changes request containing file path and instruction.

        Returns:
            A GenerateChangesResponse with a valid ChangeSet.
        """
        file_changes = self._generate_realistic_changes(request)

        if request.file_path:
            summary = f"Create helper and config modules, update {os.path.basename(request.file_path)}"
            message = f"Generated {len(file_changes)} changes: 2 new files + 1 modification"
        else:
            summary = "Create helper and config modules"
            message = f"Generated {len(file_changes)} new files"

        change_set = ChangeSet(
            changes=file_changes,
            summary=summary
        )

        return GenerateChangesResponse(
            success=True,
            change_set=change_set,
            message=message
        )

    def _generate_realistic_changes(self, request: GenerateChangesRequest) -> List[FileChange]:
        """
        Generate a realistic set of changes for testing.

        If file_path is provided:
        1. helper.py - new file with utility functions
        2. config.py - new file with configuration
        3. Modification to original file - add imports

        If file_path is not provided:
        1. helper.py - new file in workspace root
        2. config.py - new file in workspace root

        Args:
            request: The generate changes request.

        Returns:
            List of FileChange objects.
        """
        changes = []

        # Get the directory of the original file (or empty for workspace root)
        base_path = os.path.dirname(request.file_path) if request.file_path else ""

        # 1. Create helper.py
        helper_path = os.path.join(base_path, "helper.py") if base_path else "helper.py"
        helper_content = '''"""Helper utilities module."""


def format_output(data: str) -> str:
    """Format data for output."""
    return f"[OUTPUT] {data}"
'''
        changes.append(FileChange(
            file=helper_path,
            type=ChangeType.CREATE_FILE,
            content=helper_content
        ))

        # 2. Create config.py
        config_path = os.path.join(base_path, "config.py") if base_path else "config.py"
        config_content = '''"""Configuration module."""

# Application settings
DEBUG = True
VERSION = "1.0.0"
'''
        changes.append(FileChange(
            file=config_path,
            type=ChangeType.CREATE_FILE,
            content=config_content
        ))

        # 3. Modify original file - add imports at the top (only if file_path provided)
        if request.file_path:
            import_content = '''from helper import format_output
from config import DEBUG, VERSION

'''
            changes.append(FileChange(
                file=request.file_path,
                type=ChangeType.REPLACE_RANGE,
                range=Range(start=1, end=1),
                content=import_content,
                original_content=self._get_first_line(request.file_content)
            ))

        return changes

    def _get_first_line(self, file_content: Optional[str]) -> Optional[str]:
        """
        Extract the first line of file content.

        Args:
            file_content: The full file content, or None.

        Returns:
            First line of the file content, or None.
        """
        if file_content is None:
            return None

        lines = file_content.split("\n")
        return lines[0] if lines else None


# Singleton instance for convenience
mock_agent = MockAgent()

