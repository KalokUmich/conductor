"""Code generation router for AI-powered file changes.

This module provides the /generate-changes endpoint that generates
code modifications based on natural language instructions.

Currently uses MockAgent for testing. Future versions will integrate
with LLM providers (OpenAI, Anthropic, etc.) for actual AI generation.
"""
from fastapi import APIRouter

from .mock_agent import mock_agent
from .schemas import GenerateChangesRequest, GenerateChangesResponse

router = APIRouter(prefix="/generate-changes", tags=["agent"])


@router.post("", response_model=GenerateChangesResponse)
async def generate_changes(request: GenerateChangesRequest) -> GenerateChangesResponse:
    """Generate code changes based on a natural language instruction.

    This endpoint accepts a file path and instruction, then generates
    a ChangeSet containing file modifications that can be applied to
    the workspace.

    Current Implementation (MockAgent):
        - Creates helper.py and config.py files
        - Adds import statements to the target file
        - No actual LLM calls (deterministic for testing)

    Future Implementation (LLM Agent):
        - Parse instruction and analyze file content
        - Generate contextual code changes
        - Support multiple file modifications

    Args:
        request: GenerateChangesRequest containing:
            - file_path: Target file to modify (optional)
            - instruction: Natural language description of changes
            - file_content: Current file content for context (optional)

    Returns:
        GenerateChangesResponse with:
            - success: True if generation succeeded
            - change_set: ChangeSet containing file modifications
            - message: Description of what was generated

    Example:
        POST /generate-changes
        {
            "file_path": "src/main.py",
            "instruction": "Add error handling and logging"
        }
    """
    return mock_agent.generate_changes(request)

