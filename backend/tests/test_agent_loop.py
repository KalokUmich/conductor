"""Tests for the agent loop service and message format conversion."""
from __future__ import annotations

import json
import textwrap
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest

from app.ai_provider.base import AIProvider, ToolCall, ToolUseResponse
from app.ai_provider.claude_direct import _converse_to_anthropic
from app.ai_provider.openai_provider import _converse_to_openai
from app.agent_loop.prompts import build_system_prompt, scan_workspace_layout
from app.agent_loop.service import AgentLoopService, AgentResult, ThinkingStep
from app.code_tools.tools import invalidate_graph_cache


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "auth.py").write_text(textwrap.dedent("""\
        import jwt

        def authenticate(token: str) -> bool:
            try:
                payload = jwt.decode(token, "secret", algorithms=["HS256"])
                return True
            except jwt.InvalidTokenError:
                return False

        def get_user(token: str) -> dict:
            payload = jwt.decode(token, "secret", algorithms=["HS256"])
            return {"user_id": payload["sub"]}
    """))
    (tmp_path / "app" / "router.py").write_text(textwrap.dedent("""\
        from app.auth import authenticate

        def login_endpoint(request):
            token = request.headers.get("Authorization")
            if authenticate(token):
                return {"status": "ok"}
            return {"status": "unauthorized"}
    """))
    invalidate_graph_cache()
    return tmp_path


class MockProvider(AIProvider):
    """Mock AI provider that returns scripted responses."""

    def __init__(self, responses: List[ToolUseResponse]):
        self._responses = list(responses)
        self._call_count = 0

    def health_check(self) -> bool:
        return True

    def summarize(self, messages):
        return ""

    def summarize_structured(self, messages):
        pass

    def call_model(self, prompt, max_tokens=2048, system=None):
        return ""

    def chat_with_tools(self, messages, tools, max_tokens=4096, system=None, temperature=None):
        if self._call_count < len(self._responses):
            resp = self._responses[self._call_count]
            self._call_count += 1
            return resp
        # Default: end turn
        return ToolUseResponse(text="Done.", stop_reason="end_turn")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestAgentLoop:
    @pytest.mark.asyncio
    async def test_direct_answer(self, workspace):
        """Model answers immediately without using tools."""
        provider = MockProvider([
            ToolUseResponse(text="The answer is 42.", stop_reason="end_turn"),
        ])
        agent = AgentLoopService(provider=provider, max_iterations=5)
        result = await agent.run("What is the answer?", str(workspace))

        assert result.answer == "The answer is 42."
        assert result.tool_calls_made == 0
        assert result.iterations == 1
        assert result.error is None

    @pytest.mark.asyncio
    async def test_single_tool_call(self, workspace):
        """Model calls one tool then answers."""
        provider = MockProvider([
            # First response: call grep
            ToolUseResponse(
                text="Let me search for authentication code.",
                tool_calls=[ToolCall(id="tc1", name="grep", input={"pattern": "authenticate"})],
                stop_reason="tool_use",
            ),
            # Second response: answer
            ToolUseResponse(
                text="I found the authenticate function in app/auth.py.",
                stop_reason="end_turn",
            ),
        ])
        agent = AgentLoopService(provider=provider, max_iterations=5)
        result = await agent.run("How does auth work?", str(workspace))

        assert "authenticate" in result.answer
        assert result.tool_calls_made == 1
        assert result.iterations == 2

    @pytest.mark.asyncio
    async def test_multiple_tool_calls(self, workspace):
        """Model calls multiple tools across iterations."""
        provider = MockProvider([
            # Iteration 1: grep
            ToolUseResponse(
                text="",
                tool_calls=[ToolCall(id="tc1", name="grep", input={"pattern": "authenticate"})],
                stop_reason="tool_use",
            ),
            # Iteration 2: read the file
            ToolUseResponse(
                text="",
                tool_calls=[ToolCall(id="tc2", name="read_file", input={"path": "app/auth.py"})],
                stop_reason="tool_use",
            ),
            # Iteration 3: answer
            ToolUseResponse(
                text="Authentication uses JWT tokens.",
                stop_reason="end_turn",
            ),
        ])
        agent = AgentLoopService(provider=provider, max_iterations=10)
        result = await agent.run("How does auth work?", str(workspace))

        assert result.tool_calls_made == 2
        assert result.iterations == 3
        assert len(result.context_chunks) >= 1  # grep + read_file produce chunks
        # At least the read_file chunk should be present
        read_chunks = [c for c in result.context_chunks if c.source_tool == "read_file"]
        assert len(read_chunks) == 1

    @pytest.mark.asyncio
    async def test_max_iterations_reached(self, workspace):
        """Agent stops after max iterations."""
        # Provider always requests more tools
        responses = [
            ToolUseResponse(
                text="",
                tool_calls=[ToolCall(id=f"tc{i}", name="grep", input={"pattern": f"term{i}"})],
                stop_reason="tool_use",
            )
            for i in range(20)
        ]
        provider = MockProvider(responses)
        agent = AgentLoopService(provider=provider, max_iterations=3)
        result = await agent.run("test", str(workspace))

        assert result.iterations == 3
        assert result.error == "Max iterations reached"

    @pytest.mark.asyncio
    async def test_provider_error(self, workspace):
        """Agent handles provider errors gracefully."""

        class ErrorProvider(MockProvider):
            def chat_with_tools(self, *a, **kw):
                raise RuntimeError("API error")

        provider = ErrorProvider([])
        agent = AgentLoopService(provider=provider, max_iterations=5)
        result = await agent.run("test", str(workspace))

        assert result.error == "API error"
        assert result.iterations == 1

    @pytest.mark.asyncio
    async def test_tool_error_doesnt_crash(self, workspace):
        """If a tool fails, the error is passed back to the model."""
        provider = MockProvider([
            ToolUseResponse(
                text="",
                tool_calls=[ToolCall(id="tc1", name="read_file", input={"path": "nonexistent.py"})],
                stop_reason="tool_use",
            ),
            ToolUseResponse(
                text="File not found, but I can still answer.",
                stop_reason="end_turn",
            ),
        ])
        agent = AgentLoopService(provider=provider, max_iterations=5)
        result = await agent.run("Read nonexistent file", str(workspace))

        assert result.tool_calls_made == 1
        assert result.error is None
        assert "not found" in result.answer.lower() or result.answer != ""

    @pytest.mark.asyncio
    async def test_find_symbol_tool(self, workspace):
        """Agent can use find_symbol."""
        provider = MockProvider([
            ToolUseResponse(
                text="",
                tool_calls=[ToolCall(id="tc1", name="find_symbol", input={"name": "authenticate"})],
                stop_reason="tool_use",
            ),
            ToolUseResponse(
                text="Found authenticate in auth.py.",
                stop_reason="end_turn",
            ),
        ])
        agent = AgentLoopService(provider=provider, max_iterations=5)
        result = await agent.run("Where is authenticate defined?", str(workspace))

        assert result.tool_calls_made == 1
        assert result.iterations == 2

    @pytest.mark.asyncio
    async def test_multiple_tools_in_one_turn(self, workspace):
        """Model calls two tools in a single turn."""
        provider = MockProvider([
            ToolUseResponse(
                text="",
                tool_calls=[
                    ToolCall(id="tc1", name="grep", input={"pattern": "authenticate"}),
                    ToolCall(id="tc2", name="list_files", input={"directory": "app"}),
                ],
                stop_reason="tool_use",
            ),
            ToolUseResponse(
                text="Found it.",
                stop_reason="end_turn",
            ),
        ])
        agent = AgentLoopService(provider=provider, max_iterations=5)
        result = await agent.run("Find auth", str(workspace))

        assert result.tool_calls_made == 2
        assert result.iterations == 2

    @pytest.mark.asyncio
    async def test_thinking_steps_collected(self, workspace):
        """Thinking steps are accumulated across iterations."""
        provider = MockProvider([
            # Iteration 1: thinking text + tool call
            ToolUseResponse(
                text="Let me search for authentication code.",
                tool_calls=[ToolCall(id="tc1", name="grep", input={"pattern": "authenticate"})],
                stop_reason="tool_use",
            ),
            # Iteration 2: another tool call (no thinking text)
            ToolUseResponse(
                text="",
                tool_calls=[ToolCall(id="tc2", name="read_file", input={"path": "app/auth.py"})],
                stop_reason="tool_use",
            ),
            # Iteration 3: final answer
            ToolUseResponse(
                text="Authentication uses JWT tokens.",
                stop_reason="end_turn",
            ),
        ])
        agent = AgentLoopService(provider=provider, max_iterations=10)
        result = await agent.run("How does auth work?", str(workspace))

        assert result.tool_calls_made == 2
        # Should have: 1 thinking + 1 tool_call + 1 tool_result + 1 tool_call + 1 tool_result = 5
        assert len(result.thinking_steps) == 5
        kinds = [s.kind for s in result.thinking_steps]
        assert kinds[0] == "thinking"
        assert kinds[1] == "tool_call"
        assert kinds[2] == "tool_result"
        assert kinds[3] == "tool_call"
        assert kinds[4] == "tool_result"
        # Verify thinking text
        assert "authentication" in result.thinking_steps[0].text.lower()
        # Verify tool names
        assert result.thinking_steps[1].tool == "grep"
        assert result.thinking_steps[3].tool == "read_file"

    @pytest.mark.asyncio
    async def test_thinking_steps_empty_for_direct_answer(self, workspace):
        """No thinking steps when model answers immediately."""
        provider = MockProvider([
            ToolUseResponse(text="The answer is 42.", stop_reason="end_turn"),
        ])
        agent = AgentLoopService(provider=provider, max_iterations=5)
        result = await agent.run("What is the answer?", str(workspace))

        assert result.thinking_steps == []

    @pytest.mark.asyncio
    async def test_budget_note_injected(self, workspace):
        """Iteration budget note is injected after tool results."""
        call_log = []
        original_chat = MockProvider.chat_with_tools

        class TrackingProvider(MockProvider):
            def chat_with_tools(self, messages, tools, max_tokens=4096, system=None, temperature=None):
                call_log.append(messages)
                return original_chat(self, messages, tools, max_tokens, system)

        provider = TrackingProvider([
            ToolUseResponse(
                text="",
                tool_calls=[ToolCall(id="tc1", name="grep", input={"pattern": "authenticate"})],
                stop_reason="tool_use",
            ),
            ToolUseResponse(text="Found it.", stop_reason="end_turn"),
        ])
        agent = AgentLoopService(provider=provider, max_iterations=5)
        await agent.run("Find auth", str(workspace))

        # The second call should have the budget note in the messages
        assert len(call_log) >= 2
        last_user_msg = call_log[1][-1]
        assert last_user_msg["role"] == "user"
        # Should contain both toolResult and text (budget note)
        content_kinds = {
            list(block.keys())[0] for block in last_user_msg["content"]
        }
        assert "toolResult" in content_kinds
        assert "text" in content_kinds
        # Budget text should mention iteration count
        text_blocks = [b["text"] for b in last_user_msg["content"] if "text" in b]
        assert any("Iteration 1/" in t for t in text_blocks)

    @pytest.mark.asyncio
    async def test_budget_includes_max_iterations(self, workspace):
        """System prompt includes the configured max_iterations."""
        call_log = []

        class TrackingProvider(MockProvider):
            def chat_with_tools(self, messages, tools, max_tokens=4096, system=None, temperature=None):
                call_log.append(system)
                return ToolUseResponse(text="Done.", stop_reason="end_turn")

        provider = TrackingProvider([])
        agent = AgentLoopService(provider=provider, max_iterations=12)
        await agent.run("test", str(workspace))

        assert call_log
        assert "12 tool-calling iterations" in call_log[0]

    @pytest.mark.asyncio
    async def test_zero_result_grep_guidance(self, workspace):
        """Zero-result grep injects recovery guidance."""
        call_log = []

        class TrackingProvider(MockProvider):
            def chat_with_tools(self, messages, tools, max_tokens=4096, system=None, temperature=None):
                call_log.append(messages)
                resp = super().chat_with_tools(messages, tools, max_tokens, system)
                return resp

        provider = TrackingProvider([
            # grep returns 0 results
            ToolUseResponse(
                text="",
                tool_calls=[ToolCall(id="tc1", name="grep", input={"pattern": "nonexistent_xyz"})],
                stop_reason="tool_use",
            ),
            ToolUseResponse(text="Not found.", stop_reason="end_turn"),
        ])
        agent = AgentLoopService(provider=provider, max_iterations=5)
        await agent.run("Find xyz", str(workspace))

        # Second LLM call should see zero-result guidance
        assert len(call_log) >= 2
        last_user_msg = call_log[1][-1]
        text_blocks = [b["text"] for b in last_user_msg["content"] if "text" in b]
        combined = " ".join(text_blocks)
        assert "0 results" in combined
        assert "find_symbol" in combined

    @pytest.mark.asyncio
    async def test_scatter_detection(self, workspace):
        """Reading files from 5+ directories triggers scatter warning."""
        # Create files in many different directories
        for d in ["svc_a", "svc_b", "svc_c", "svc_d", "svc_e"]:
            (workspace / d).mkdir(exist_ok=True)
            (workspace / d / "mod.py").write_text(f"# {d}")

        call_log = []

        class TrackingProvider(MockProvider):
            def chat_with_tools(self, messages, tools, max_tokens=4096, system=None, temperature=None):
                call_log.append(messages)
                resp = super().chat_with_tools(messages, tools, max_tokens, system)
                return resp

        provider = TrackingProvider([
            # Read files from 5 different directories
            ToolUseResponse(
                text="",
                tool_calls=[
                    ToolCall(id="tc1", name="read_file", input={"path": "svc_a/mod.py"}),
                    ToolCall(id="tc2", name="read_file", input={"path": "svc_b/mod.py"}),
                    ToolCall(id="tc3", name="read_file", input={"path": "svc_c/mod.py"}),
                    ToolCall(id="tc4", name="read_file", input={"path": "svc_d/mod.py"}),
                    ToolCall(id="tc5", name="read_file", input={"path": "svc_e/mod.py"}),
                ],
                stop_reason="tool_use",
            ),
            ToolUseResponse(text="Found it.", stop_reason="end_turn"),
        ])
        agent = AgentLoopService(provider=provider, max_iterations=5)
        await agent.run("Find something", str(workspace))

        # Second LLM call should have scatter warning
        assert len(call_log) >= 2
        last_user_msg = call_log[1][-1]
        text_blocks = [b["text"] for b in last_user_msg["content"] if "text" in b]
        combined = " ".join(text_blocks)
        assert "SCATTER WARNING" in combined

    @pytest.mark.asyncio
    async def test_evidence_retry_cap(self, workspace):
        """Evidence retries are capped at max_evidence_retries to prevent dead loops.

        Simulates a model that always gives vague answers without file:line refs.
        Without the cap, the agent loop would retry indefinitely until budget runs out.
        When the cap is hit, the answer is enriched with files the agent accessed.
        """
        vague_answer = (
            "The authentication system works by checking user credentials against "
            "the database. It uses a service layer pattern where the controller "
            "delegates to a service which calls the repository."
        )
        # First response: a tool call to ensure tool_calls_made >= 2 and files_accessed >= 1
        provider = MockProvider([
            ToolUseResponse(
                text="Let me look.",
                tool_calls=[ToolCall(id="t1", name="grep", input={"pattern": "auth"})],
                stop_reason="tool_use",
            ),
            ToolUseResponse(
                text="Let me read.",
                tool_calls=[ToolCall(id="t2", name="read_file", input={"path": "app/auth.py"})],
                stop_reason="tool_use",
            ),
            # Three consecutive vague answers — only 2 retries allowed
            ToolUseResponse(text=vague_answer, stop_reason="end_turn"),
            ToolUseResponse(text=vague_answer, stop_reason="end_turn"),
            ToolUseResponse(text=vague_answer, stop_reason="end_turn"),
        ])
        agent = AgentLoopService(
            provider=provider,
            max_iterations=10,
            max_evidence_retries=2,
        )
        result = await agent.run("How does auth work?", str(workspace))

        # After 2 retries, the answer should be enriched with file refs
        assert vague_answer in result.answer
        assert "Files examined during analysis" in result.answer
        # Should have used exactly: 2 tool calls + 3 answer attempts = 5 LLM calls
        assert result.iterations == 5


# ---------------------------------------------------------------------------
# Completeness verifier tests
# ---------------------------------------------------------------------------


# TestCompletenessVerifier removed — completeness check replaced by Brain review.

class TestCompletenessModule:
    """Unit tests for the completeness module itself."""

    @pytest.mark.asyncio
    async def test_parse_sufficient(self):
        from app.agent_loop.completeness import _parse_response
        result = _parse_response('{"sufficient": true}')
        assert result.sufficient is True
        assert result.hints == []

    @pytest.mark.asyncio
    async def test_parse_insufficient(self):
        from app.agent_loop.completeness import _parse_response
        result = _parse_response('{"sufficient": false, "hints": ["Check SQL", "Trace appeal"]}')
        assert result.sufficient is False
        assert len(result.hints) == 2
        assert "Check SQL" in result.hints

    @pytest.mark.asyncio
    async def test_parse_code_block_wrapped(self):
        from app.agent_loop.completeness import _parse_response
        result = _parse_response('```json\n{"sufficient": false, "hints": ["Look deeper"]}\n```')
        assert result.sufficient is False
        assert result.hints == ["Look deeper"]

    @pytest.mark.asyncio
    async def test_parse_invalid_json_defaults_sufficient(self):
        from app.agent_loop.completeness import _parse_response
        result = _parse_response("I think this is sufficient")
        assert result.sufficient is True
        assert result.hints == []

    @pytest.mark.asyncio
    async def test_build_tool_summary(self):
        from app.agent_loop.completeness import _build_tool_summary
        history = [
            {"tool": "grep", "params": {"pattern": "auth"}, "summary": "5 results"},
            {"tool": "read_file", "params": {"path": "auth.py"}, "summary": "42 lines"},
        ]
        summary = _build_tool_summary(history)
        assert "grep" in summary
        assert "read_file" in summary
        assert "auth" in summary

    @pytest.mark.asyncio
    async def test_build_tool_summary_empty(self):
        from app.agent_loop.completeness import _build_tool_summary
        assert _build_tool_summary([]) == "(none)"

    @pytest.mark.asyncio
    async def test_build_tool_summary_truncation(self):
        from app.agent_loop.completeness import _build_tool_summary
        history = [{"tool": f"tool_{i}", "params": {}, "summary": ""} for i in range(50)]
        summary = _build_tool_summary(history, max_entries=5)
        assert "tool_0" in summary
        assert "tool_4" in summary
        assert "45 more" in summary


# ---------------------------------------------------------------------------
# Message format conversion tests
# ---------------------------------------------------------------------------


class TestConverseToAnthropic:
    """Test Bedrock Converse → Anthropic Messages API format conversion."""

    def test_plain_user_message(self):
        msgs = [{"role": "user", "content": [{"text": "Hello"}]}]
        result = _converse_to_anthropic(msgs)
        assert result == [{"role": "user", "content": [{"type": "text", "text": "Hello"}]}]

    def test_string_content_passthrough(self):
        msgs = [{"role": "user", "content": "Hello"}]
        result = _converse_to_anthropic(msgs)
        assert result == [{"role": "user", "content": "Hello"}]

    def test_assistant_with_tool_use(self):
        msgs = [
            {
                "role": "assistant",
                "content": [
                    {"text": "Let me search."},
                    {"toolUse": {"toolUseId": "tc1", "name": "grep", "input": {"pattern": "auth"}}},
                ],
            }
        ]
        result = _converse_to_anthropic(msgs)
        assert len(result) == 1
        blocks = result[0]["content"]
        assert blocks[0] == {"type": "text", "text": "Let me search."}
        assert blocks[1] == {
            "type": "tool_use",
            "id": "tc1",
            "name": "grep",
            "input": {"pattern": "auth"},
        }

    def test_tool_result(self):
        msgs = [
            {
                "role": "user",
                "content": [
                    {
                        "toolResult": {
                            "toolUseId": "tc1",
                            "content": [{"text": '{"matches": []}'}],
                        }
                    }
                ],
            }
        ]
        result = _converse_to_anthropic(msgs)
        assert len(result) == 1
        blocks = result[0]["content"]
        assert blocks[0]["type"] == "tool_result"
        assert blocks[0]["tool_use_id"] == "tc1"
        assert blocks[0]["content"] == '{"matches": []}'

    def test_multiple_tool_results_in_one_message(self):
        msgs = [
            {
                "role": "user",
                "content": [
                    {"toolResult": {"toolUseId": "tc1", "content": [{"text": "result1"}]}},
                    {"toolResult": {"toolUseId": "tc2", "content": [{"text": "result2"}]}},
                ],
            }
        ]
        result = _converse_to_anthropic(msgs)
        blocks = result[0]["content"]
        assert len(blocks) == 2
        assert blocks[0]["tool_use_id"] == "tc1"
        assert blocks[1]["tool_use_id"] == "tc2"

    def test_full_conversation_round_trip(self):
        """Simulate a full agent loop conversation."""
        msgs = [
            {"role": "user", "content": [{"text": "How does auth work?"}]},
            {
                "role": "assistant",
                "content": [
                    {"text": "Let me search."},
                    {"toolUse": {"toolUseId": "tc1", "name": "grep", "input": {"pattern": "auth"}}},
                ],
            },
            {
                "role": "user",
                "content": [
                    {"toolResult": {"toolUseId": "tc1", "content": [{"text": "found in auth.py"}]}},
                ],
            },
        ]
        result = _converse_to_anthropic(msgs)
        assert len(result) == 3
        assert result[0]["content"][0]["type"] == "text"
        assert result[1]["content"][1]["type"] == "tool_use"
        assert result[2]["content"][0]["type"] == "tool_result"


class TestConverseToOpenAI:
    """Test Bedrock Converse → OpenAI Chat Completions format conversion."""

    def test_plain_user_message(self):
        msgs = [{"role": "user", "content": [{"text": "Hello"}]}]
        result = _converse_to_openai(msgs)
        assert result == [{"role": "user", "content": "Hello"}]

    def test_string_content_passthrough(self):
        msgs = [{"role": "user", "content": "Hello"}]
        result = _converse_to_openai(msgs)
        assert result == [{"role": "user", "content": "Hello"}]

    def test_assistant_with_tool_calls(self):
        msgs = [
            {
                "role": "assistant",
                "content": [
                    {"text": "Let me search."},
                    {"toolUse": {"toolUseId": "tc1", "name": "grep", "input": {"pattern": "auth"}}},
                ],
            }
        ]
        result = _converse_to_openai(msgs)
        assert len(result) == 1
        msg = result[0]
        assert msg["role"] == "assistant"
        assert msg["content"] == "Let me search."
        assert len(msg["tool_calls"]) == 1
        tc = msg["tool_calls"][0]
        assert tc["id"] == "tc1"
        assert tc["type"] == "function"
        assert tc["function"]["name"] == "grep"
        assert json.loads(tc["function"]["arguments"]) == {"pattern": "auth"}

    def test_assistant_no_text_with_tool_calls(self):
        msgs = [
            {
                "role": "assistant",
                "content": [
                    {"toolUse": {"toolUseId": "tc1", "name": "grep", "input": {"pattern": "x"}}},
                ],
            }
        ]
        result = _converse_to_openai(msgs)
        assert result[0]["content"] is None
        assert len(result[0]["tool_calls"]) == 1

    def test_tool_results_become_separate_messages(self):
        msgs = [
            {
                "role": "user",
                "content": [
                    {"toolResult": {"toolUseId": "tc1", "content": [{"text": "result1"}]}},
                    {"toolResult": {"toolUseId": "tc2", "content": [{"text": "result2"}]}},
                ],
            }
        ]
        result = _converse_to_openai(msgs)
        assert len(result) == 2
        assert result[0] == {"role": "tool", "tool_call_id": "tc1", "content": "result1"}
        assert result[1] == {"role": "tool", "tool_call_id": "tc2", "content": "result2"}

    def test_multiple_tool_calls_in_one_assistant_message(self):
        msgs = [
            {
                "role": "assistant",
                "content": [
                    {"toolUse": {"toolUseId": "tc1", "name": "grep", "input": {"pattern": "a"}}},
                    {"toolUse": {"toolUseId": "tc2", "name": "list_files", "input": {"directory": "."}}},
                ],
            }
        ]
        result = _converse_to_openai(msgs)
        assert len(result) == 1
        assert len(result[0]["tool_calls"]) == 2

    def test_full_conversation_round_trip(self):
        """Simulate a full agent loop conversation."""
        msgs = [
            {"role": "user", "content": [{"text": "How does auth work?"}]},
            {
                "role": "assistant",
                "content": [
                    {"text": "Searching..."},
                    {"toolUse": {"toolUseId": "tc1", "name": "grep", "input": {"pattern": "auth"}}},
                ],
            },
            {
                "role": "user",
                "content": [
                    {"toolResult": {"toolUseId": "tc1", "content": [{"text": "found it"}]}},
                ],
            },
        ]
        result = _converse_to_openai(msgs)
        assert len(result) == 3
        # User message → plain content
        assert result[0] == {"role": "user", "content": "How does auth work?"}
        # Assistant → tool_calls
        assert result[1]["role"] == "assistant"
        assert result[1]["tool_calls"][0]["function"]["name"] == "grep"
        # Tool result → role: tool
        assert result[2]["role"] == "tool"
        assert result[2]["tool_call_id"] == "tc1"


# ---------------------------------------------------------------------------
# Workspace layout scanning + prompt tests
# ---------------------------------------------------------------------------


class TestScanWorkspaceLayout:
    """Tests for scan_workspace_layout — project structure discovery."""

    def test_flat_project(self, tmp_path: Path):
        """Simple project with files at root."""
        (tmp_path / "main.py").write_text("print('hi')")
        (tmp_path / "requirements.txt").write_text("flask")
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "app.py").write_text("# app")

        layout = scan_workspace_layout(str(tmp_path))
        assert "requirements.txt" in layout
        assert "src/" in layout
        assert "Detected project roots" in layout
        assert "(root)" in layout  # marker at root level

    def test_nested_project_detected(self, tmp_path: Path):
        """Repo where source is nested under a subdirectory (abound-server scenario)."""
        nested = tmp_path / "abound-server"
        nested.mkdir()
        (nested / "pom.xml").write_text("<project/>")
        src = nested / "src" / "main" / "java"
        src.mkdir(parents=True)
        (src / "App.java").write_text("public class App {}")
        # Also a README at root
        (tmp_path / "README.md").write_text("# Docs")

        layout = scan_workspace_layout(str(tmp_path))
        assert "abound-server/" in layout
        assert "pom.xml" in layout
        assert "Detected project roots" in layout
        # The nested dir should be identified as a project root
        assert "abound-server" in layout

    def test_multiple_project_roots(self, tmp_path: Path):
        """Monorepo with multiple project roots."""
        frontend = tmp_path / "frontend"
        frontend.mkdir()
        (frontend / "package.json").write_text("{}")
        (frontend / "src").mkdir()

        backend = tmp_path / "backend"
        backend.mkdir()
        (backend / "requirements.txt").write_text("fastapi")
        (backend / "app").mkdir()

        layout = scan_workspace_layout(str(tmp_path))
        assert "frontend/" in layout
        assert "backend/" in layout
        assert "package.json" in layout
        assert "requirements.txt" in layout

    def test_excluded_dirs_skipped(self, tmp_path: Path):
        """node_modules and .git should not appear in layout."""
        (tmp_path / "node_modules").mkdir()
        (tmp_path / "node_modules" / "lodash").mkdir()
        (tmp_path / ".git").mkdir()
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "index.ts").write_text("// hi")

        layout = scan_workspace_layout(str(tmp_path))
        assert "node_modules" not in layout
        assert ".git" not in layout
        assert "src/" in layout

    def test_empty_workspace(self, tmp_path: Path):
        """Empty directory returns empty layout."""
        layout = scan_workspace_layout(str(tmp_path))
        # No project markers, minimal tree
        assert "Detected project roots" not in layout

    def test_nonexistent_path(self):
        """Non-existent workspace returns empty string."""
        layout = scan_workspace_layout("/nonexistent/path/xyz")
        assert layout == ""

    def test_max_entries_respected(self, tmp_path: Path):
        """Layout is truncated when too many entries."""
        for i in range(100):
            (tmp_path / f"file_{i:03d}.txt").write_text(f"content {i}")

        layout = scan_workspace_layout(str(tmp_path), max_entries=10)
        assert "truncated" in layout


class TestBuildSystemPrompt:
    """Tests for build_system_prompt with workspace layout injection."""

    def test_includes_workspace_path(self, tmp_path: Path):
        prompt = build_system_prompt(str(tmp_path))
        assert str(tmp_path) in prompt

    def test_includes_layout_section(self, tmp_path: Path):
        (tmp_path / "package.json").write_text("{}")
        (tmp_path / "src").mkdir()

        prompt = build_system_prompt(str(tmp_path))
        assert "Directory layout" in prompt
        assert "package.json" in prompt
        assert "How to investigate" in prompt

    def test_precomputed_layout(self, tmp_path: Path):
        """Passing pre-computed layout skips scanning."""
        prompt = build_system_prompt(str(tmp_path), workspace_layout="CUSTOM_LAYOUT_HERE")
        assert "CUSTOM_LAYOUT_HERE" in prompt

    def test_code_review_strategy_injected(self, tmp_path: Path):
        """Code review is the only query type that injects a strategy template."""
        prompt = build_system_prompt(str(tmp_path), query_type="code_review")
        assert "Code Review" in prompt

    def test_non_review_queries_have_no_strategy(self, tmp_path: Path):
        """Non-review queries should NOT inject prescriptive strategies."""
        for qt in ("root_cause_analysis", "architecture_question", "business_flow_tracing"):
            prompt = build_system_prompt(str(tmp_path), query_type=qt)
            assert "## Strategy" not in prompt
            assert "## Goal" not in prompt

    def test_interactive_mode_adds_clarification_step(self, tmp_path: Path):
        """Interactive mode injects ask_user guidance into investigation flow."""
        prompt = build_system_prompt(str(tmp_path), interactive=True)
        assert "ask_user" in prompt
        # Guidance should be INSIDE "How to investigate", not a separate section
        how_idx = prompt.index("How to investigate")
        ask_idx = prompt.index("ask_user")
        angles_idx = prompt.index("multiple angles")
        assert how_idx < ask_idx < angles_idx, "ask_user guidance should be between 'How to investigate' and 'multiple angles'"

    def test_non_interactive_no_ask_user(self, tmp_path: Path):
        """Non-interactive mode has no ask_user guidance."""
        prompt = build_system_prompt(str(tmp_path), interactive=False)
        assert "ask_user" not in prompt


# TestMultiPerspective removed — multi-perspective exploration is now
# handled by Brain's dispatch_swarm("business_flow"), not AgentLoopService.


# ---------------------------------------------------------------------------
# _clear_old_tool_results with ToolMetadata-driven summaries
# ---------------------------------------------------------------------------


class TestClearOldToolResults:
    """Tests for the metadata-driven context compaction."""

    def test_clears_old_results_beyond_cutoff(self):
        from app.agent_loop.service import _clear_old_tool_results
        messages = self._build_messages(8)  # 4 turn-pairs
        _clear_old_tool_results(messages, keep_recent=2)
        # First 2 turn-pairs should be cleared, last 2 kept
        cleared = [m for m in messages if m.get("role") == "user"
                   and isinstance(m.get("content"), list)
                   and any(b.get("toolResult", {}).get("content", [{}])[0].get("text", "").startswith("[cleared]")
                           for b in m["content"] if "toolResult" in b)]
        assert len(cleared) == 2

    def test_preserves_recent_results(self):
        from app.agent_loop.service import _clear_old_tool_results
        messages = self._build_messages(8)
        _clear_old_tool_results(messages, keep_recent=2)
        # Last 2 user messages should NOT be cleared
        last_user = [m for m in messages[-4:] if m.get("role") == "user"]
        for m in last_user:
            for block in m.get("content", []):
                tr = block.get("toolResult", {})
                inner = tr.get("content", [{}])
                if inner:
                    assert not inner[0].get("text", "").startswith("[cleared]")

    def test_metadata_summary_used_for_grep(self):
        from app.agent_loop.service import _clear_old_tool_results
        messages = self._build_messages_with_tool("grep", {"pattern": "auth", "path": "src/"}, 6)
        _clear_old_tool_results(messages, keep_recent=1)
        # Check that the cleared message uses the summary template
        user_msg = messages[1]  # first user message (should be cleared)
        text = user_msg["content"][0]["toolResult"]["content"][0]["text"]
        assert text.startswith("[cleared]")
        assert "grep" in text
        assert "auth" in text

    def test_fallback_when_no_tool_use_match(self):
        from app.agent_loop.service import _clear_old_tool_results
        # Build messages without matching assistant tool_use blocks
        messages = [
            {"role": "user", "content": [{"toolResult": {
                "toolUseId": "orphan-id",
                "content": [{"text": '{"some": "data"}'}],
            }}]},
            {"role": "assistant", "content": [{"text": "reply"}]},
            {"role": "user", "content": [{"text": "next question"}]},
            {"role": "assistant", "content": [{"text": "next reply"}]},
        ]
        _clear_old_tool_results(messages, keep_recent=1)
        text = messages[0]["content"][0]["toolResult"]["content"][0]["text"]
        assert text.startswith("[cleared]")
        # Fallback: should use first_line + chars pattern
        assert "chars)" in text

    def test_too_few_messages_no_clearing(self):
        from app.agent_loop.service import _clear_old_tool_results
        messages = self._build_messages(4)  # 2 turn-pairs
        original_texts = []
        for m in messages:
            if m.get("role") == "user":
                for b in m.get("content", []):
                    tr = b.get("toolResult", {})
                    inner = tr.get("content", [])
                    if inner:
                        original_texts.append(inner[0].get("text", ""))
        _clear_old_tool_results(messages, keep_recent=4)
        # Nothing should be cleared
        for m in messages:
            if m.get("role") == "user":
                for b in m.get("content", []):
                    tr = b.get("toolResult", {})
                    inner = tr.get("content", [])
                    if inner:
                        assert not inner[0].get("text", "").startswith("[cleared]")

    def test_already_cleared_not_double_cleared(self):
        from app.agent_loop.service import _clear_old_tool_results
        messages = self._build_messages(8)
        _clear_old_tool_results(messages, keep_recent=2)
        # Get the cleared text
        cleared_text = messages[1]["content"][0]["toolResult"]["content"][0]["text"]
        # Run again — should not re-clear
        _clear_old_tool_results(messages, keep_recent=2)
        assert messages[1]["content"][0]["toolResult"]["content"][0]["text"] == cleared_text

    # --- Helper methods ---

    @staticmethod
    def _build_messages(count: int) -> list:
        """Build alternating assistant/user messages for testing."""
        messages = []
        for i in range(count):
            if i % 2 == 0:
                # Assistant message with tool_use
                messages.append({
                    "role": "assistant",
                    "content": [{
                        "toolUse": {
                            "toolUseId": f"tu-{i}",
                            "name": "grep",
                            "input": {"pattern": "test", "path": "."},
                        }
                    }],
                })
            else:
                # User message with toolResult
                messages.append({
                    "role": "user",
                    "content": [{
                        "toolResult": {
                            "toolUseId": f"tu-{i-1}",
                            "content": [{"text": json.dumps([
                                {"file_path": f"file{i}.py", "line_number": i, "content": f"match {i}"},
                            ])}],
                        }
                    }],
                })
        return messages

    @staticmethod
    def _build_messages_with_tool(tool_name: str, params: dict, count: int) -> list:
        """Build messages with a specific tool for summary testing."""
        messages = []
        for i in range(count):
            if i % 2 == 0:
                messages.append({
                    "role": "assistant",
                    "content": [{
                        "toolUse": {
                            "toolUseId": f"tu-{i}",
                            "name": tool_name,
                            "input": params,
                        }
                    }],
                })
            else:
                messages.append({
                    "role": "user",
                    "content": [{
                        "toolResult": {
                            "toolUseId": f"tu-{i-1}",
                            "content": [{"text": json.dumps([
                                {"file_path": "src/auth.py", "line_number": 10, "content": "def authenticate()"},
                                {"file_path": "src/auth.py", "line_number": 20, "content": "def verify_token()"},
                            ])}],
                        }
                    }],
                })
        return messages
