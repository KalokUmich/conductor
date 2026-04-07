"""Tests for Bedrock tool call repair and schema sanitization."""

from unittest.mock import MagicMock, patch

from app.ai_provider.base import ToolCall
from app.ai_provider.claude_bedrock import (
    ClaudeBedrockProvider,
    _build_param_registry,
    _extract_kv_pairs,
    _extract_tool_calls_from_text,
    _extract_xml_tool_calls,
    _parse_malformed_name,
    _repair_tool_calls,
    _sanitize_schema,
    _validate_params,
)

KNOWN_TOOLS = {
    "grep",
    "read_file",
    "list_files",
    "find_symbol",
    "find_references",
    "file_outline",
    "get_dependencies",
    "get_dependents",
    "git_log",
    "git_diff",
    "ast_search",
    "get_callees",
    "get_callers",
    "git_blame",
    "git_show",
    "find_tests",
    "test_outline",
    "trace_variable",
    "compressed_view",
    "module_summary",
    "expand_symbol",
}

# Minimal tool definitions for schema-aware tests
TOOL_DEFS = [
    {
        "name": "grep",
        "description": "Search files",
        "input_schema": {
            "type": "object",
            "required": ["pattern"],
            "properties": {
                "pattern": {"type": "string"},
                "path": {"type": "string"},
                "include_glob": {"type": "string"},
                "max_results": {"type": "integer"},
            },
        },
    },
    {
        "name": "read_file",
        "description": "Read a file",
        "input_schema": {
            "type": "object",
            "required": ["path"],
            "properties": {
                "path": {"type": "string"},
                "start_line": {"type": "integer"},
                "end_line": {"type": "integer"},
            },
        },
    },
    {
        "name": "find_symbol",
        "description": "Find symbol definitions",
        "input_schema": {
            "type": "object",
            "required": ["name"],
            "properties": {
                "name": {"type": "string"},
                "kind": {"type": "string"},
            },
        },
    },
    {
        "name": "module_summary",
        "description": "Module summary",
        "input_schema": {
            "type": "object",
            "required": ["module_path"],
            "properties": {
                "module_path": {"type": "string"},
            },
        },
    },
    {
        "name": "git_log",
        "description": "Show git log",
        "input_schema": {
            "type": "object",
            "properties": {
                "file": {"type": "string"},
                "n": {"type": "integer"},
            },
        },
    },
]


# ---------------------------------------------------------------------------
# _sanitize_schema
# ---------------------------------------------------------------------------


class TestSanitizeSchema:
    def test_removes_top_level_title(self):
        schema = {"title": "GrepParams", "type": "object", "properties": {}}
        result = _sanitize_schema(schema)
        assert "title" not in result

    def test_removes_defs(self):
        schema = {"$defs": {"Foo": {}}, "type": "object", "properties": {}}
        result = _sanitize_schema(schema)
        assert "$defs" not in result

    def test_removes_definitions(self):
        schema = {"definitions": {"Bar": {}}, "type": "object", "properties": {}}
        result = _sanitize_schema(schema)
        assert "definitions" not in result

    def test_converts_anyof_optional_to_type(self):
        schema = {
            "type": "object",
            "properties": {
                "path": {
                    "anyOf": [{"type": "string"}, {"type": "null"}],
                    "title": "Path",
                    "default": None,
                }
            },
        }
        result = _sanitize_schema(schema)
        prop = result["properties"]["path"]
        assert "anyOf" not in prop
        assert prop["type"] == "string"
        assert "title" not in prop

    def test_preserves_required_fields(self):
        schema = {
            "type": "object",
            "required": ["pattern"],
            "properties": {
                "pattern": {"type": "string"},
            },
        }
        result = _sanitize_schema(schema)
        assert result["required"] == ["pattern"]
        assert result["properties"]["pattern"]["type"] == "string"

    def test_removes_per_property_title(self):
        schema = {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "title": "Pattern"},
                "path": {"type": "string", "title": "Path"},
            },
        }
        result = _sanitize_schema(schema)
        for prop in result["properties"].values():
            assert "title" not in prop

    def test_does_not_mutate_original(self):
        original = {
            "title": "Foo",
            "type": "object",
            "properties": {
                "x": {"anyOf": [{"type": "integer"}, {"type": "null"}], "title": "X"},
            },
        }
        import copy

        before = copy.deepcopy(original)
        _sanitize_schema(original)
        assert original == before

    def test_handles_nested_object_properties(self):
        schema = {
            "type": "object",
            "properties": {
                "config": {
                    "type": "object",
                    "title": "Config",
                    "properties": {
                        "timeout": {
                            "anyOf": [{"type": "integer"}, {"type": "null"}],
                            "title": "Timeout",
                        }
                    },
                }
            },
        }
        result = _sanitize_schema(schema)
        nested = result["properties"]["config"]["properties"]["timeout"]
        assert nested["type"] == "integer"
        assert "anyOf" not in nested
        assert "title" not in nested

    def test_handles_array_items(self):
        schema = {
            "type": "object",
            "properties": {
                "tags": {
                    "type": "array",
                    "items": {
                        "anyOf": [{"type": "string"}, {"type": "null"}],
                        "title": "Tag",
                    },
                }
            },
        }
        result = _sanitize_schema(schema)
        items = result["properties"]["tags"]["items"]
        assert items["type"] == "string"
        assert "anyOf" not in items

    def test_all_null_anyof_becomes_string(self):
        schema = {
            "type": "object",
            "properties": {
                "x": {"anyOf": [{"type": "null"}]},
            },
        }
        result = _sanitize_schema(schema)
        assert result["properties"]["x"]["type"] == "string"

    def test_empty_schema(self):
        result = _sanitize_schema({})
        assert result == {}

    def test_real_pydantic_grep_schema(self):
        """Simulate a real Pydantic v2 schema for the grep tool."""
        schema = {
            "title": "GrepParams",
            "$defs": {},
            "type": "object",
            "required": ["pattern"],
            "properties": {
                "pattern": {"type": "string", "title": "Pattern", "description": "Regex pattern"},
                "include_glob": {
                    "anyOf": [{"type": "string"}, {"type": "null"}],
                    "title": "Include Glob",
                    "default": None,
                    "description": "Glob filter",
                },
                "path": {
                    "anyOf": [{"type": "string"}, {"type": "null"}],
                    "title": "Path",
                    "default": None,
                },
                "max_results": {
                    "anyOf": [{"type": "integer"}, {"type": "null"}],
                    "title": "Max Results",
                    "default": None,
                },
            },
        }
        result = _sanitize_schema(schema)
        assert "title" not in result
        assert "$defs" not in result
        assert result["properties"]["pattern"]["type"] == "string"
        assert result["properties"]["include_glob"]["type"] == "string"
        assert result["properties"]["path"]["type"] == "string"
        assert result["properties"]["max_results"]["type"] == "integer"
        for prop in result["properties"].values():
            assert "anyOf" not in prop
            assert "title" not in prop


# ---------------------------------------------------------------------------
# _build_param_registry
# ---------------------------------------------------------------------------


class TestBuildParamRegistry:
    def test_builds_registry(self):
        registry = _build_param_registry(TOOL_DEFS)
        assert registry["grep"] == {"pattern", "path", "include_glob", "max_results"}
        assert registry["read_file"] == {"path", "start_line", "end_line"}
        assert registry["module_summary"] == {"module_path"}

    def test_empty_defs(self):
        assert _build_param_registry([]) == {}

    def test_tool_without_schema(self):
        defs = [{"name": "custom_tool"}]
        registry = _build_param_registry(defs)
        assert registry["custom_tool"] == set()


# ---------------------------------------------------------------------------
# _validate_params
# ---------------------------------------------------------------------------


class TestValidateParams:
    def test_keeps_valid_params(self):
        registry = _build_param_registry(TOOL_DEFS)
        params = {"pattern": "foo", "path": "src"}
        result = _validate_params(params, "grep", registry)
        assert result == {"pattern": "foo", "path": "src"}

    def test_drops_invalid_params(self):
        registry = _build_param_registry(TOOL_DEFS)
        params = {"pattern": "foo", "name": "bar", "module_path": "baz"}
        result = _validate_params(params, "grep", registry)
        assert result == {"pattern": "foo"}
        assert "name" not in result
        assert "module_path" not in result

    def test_unknown_tool_passes_through(self):
        registry = _build_param_registry(TOOL_DEFS)
        params = {"x": 1, "y": 2}
        result = _validate_params(params, "unknown_tool", registry)
        assert result == {"x": 1, "y": 2}

    def test_empty_params(self):
        registry = _build_param_registry(TOOL_DEFS)
        assert _validate_params({}, "grep", registry) == {}


# ---------------------------------------------------------------------------
# _extract_kv_pairs
# ---------------------------------------------------------------------------


class TestExtractKvPairs:
    def test_basic_pairs(self):
        text = 'pattern="render" path="CDE"'
        assert _extract_kv_pairs(text) == {"pattern": "render", "path": "CDE"}

    def test_numeric_value_quoted(self):
        text = 'max_results="50"'
        assert _extract_kv_pairs(text) == {"max_results": 50}

    def test_numeric_value_unquoted(self):
        text = "max_results=50>"
        assert _extract_kv_pairs(text) == {"max_results": 50}

    def test_unquoted_at_end_of_string(self):
        text = "n=10"
        assert _extract_kv_pairs(text) == {"n": 10}

    def test_mixed_quoted_and_unquoted(self):
        text = 'pattern="[Ii]dv" path="backend/src" max_results=50'
        result = _extract_kv_pairs(text)
        assert result["pattern"] == "[Ii]dv"
        assert result["path"] == "backend/src"
        assert result["max_results"] == 50

    def test_no_pairs(self):
        assert _extract_kv_pairs("just some text") == {}

    def test_with_leading_junk(self):
        text = '" pattern="render|Render" include_glob="*.py"'
        result = _extract_kv_pairs(text)
        assert result["pattern"] == "render|Render"
        assert result["include_glob"] == "*.py"


# ---------------------------------------------------------------------------
# _parse_malformed_name
# ---------------------------------------------------------------------------


class TestParseMalformedName:
    def test_exact_match(self):
        name, params = _parse_malformed_name("grep", KNOWN_TOOLS)
        assert name == "grep"
        assert params == {}

    def test_name_with_params(self):
        name, params = _parse_malformed_name(
            'grep" pattern="render|Render|RENDER" include_glob="*.py" path="CDE',
            KNOWN_TOOLS,
        )
        assert name == "grep"
        assert params["pattern"] == "render|Render|RENDER"
        assert params["include_glob"] == "*.py"
        assert params["path"] == "CDE"

    def test_unknown_tool(self):
        name, params = _parse_malformed_name("unknown_tool", KNOWN_TOOLS)
        assert name is None
        assert params == {}

    def test_read_file_with_params(self):
        name, params = _parse_malformed_name(
            'read_file" path="src/main.py" start_line="1" end_line="50"',
            KNOWN_TOOLS,
        )
        assert name == "read_file"
        assert params["path"] == "src/main.py"
        assert params["start_line"] == 1
        assert params["end_line"] == 50


# ---------------------------------------------------------------------------
# _extract_xml_tool_calls
# ---------------------------------------------------------------------------


class TestExtractXmlToolCalls:
    def test_invoke_with_parameter_elements(self):
        xml = (
            '<invoke name="grep">'
            '<parameter name="pattern">auth</parameter>'
            '<parameter name="path">src</parameter>'
            "</invoke>"
        )
        calls = _extract_xml_tool_calls(xml, KNOWN_TOOLS)
        assert len(calls) == 1
        assert calls[0].name == "grep"
        assert calls[0].input == {"pattern": "auth", "path": "src"}

    def test_invoke_attribute_style(self):
        xml = '<invoke name="grep" pattern="render" path="backend/src" max_results="50"/>'
        calls = _extract_xml_tool_calls(xml, KNOWN_TOOLS)
        assert len(calls) == 1
        assert calls[0].name == "grep"
        assert calls[0].input["pattern"] == "render"
        assert calls[0].input["path"] == "backend/src"
        assert calls[0].input["max_results"] == 50

    def test_multiple_invocations(self):
        xml = (
            '<invoke name="grep"><parameter name="pattern">foo</parameter></invoke>'
            '<invoke name="read_file"><parameter name="path">bar.py</parameter></invoke>'
        )
        calls = _extract_xml_tool_calls(xml, KNOWN_TOOLS)
        assert len(calls) == 2
        assert calls[0].name == "grep"
        assert calls[1].name == "read_file"

    def test_unknown_tool_ignored(self):
        xml = '<invoke name="unknown_tool"><parameter name="x">1</parameter></invoke>'
        calls = _extract_xml_tool_calls(xml, KNOWN_TOOLS)
        assert calls == []

    def test_no_xml_returns_empty(self):
        calls = _extract_xml_tool_calls("just plain text", KNOWN_TOOLS)
        assert calls == []

    def test_garbled_name_with_xml(self):
        """The real production failure: garbled name contains XML fragments."""
        garbled = (
            'grep" pattern="[Ii][Dd][Vv]" path="backend/src" max_results=50>'
            '</invoke>\n<invoke name="module_summary">'
            '<parameter name="module_path">backend/src</parameter>'
            "</invoke>"
        )
        calls = _extract_xml_tool_calls(garbled, KNOWN_TOOLS)
        assert len(calls) == 1
        assert calls[0].name == "module_summary"
        assert calls[0].input["module_path"] == "backend/src"

    def test_numeric_parameter_value(self):
        xml = '<invoke name="git_log"><parameter name="n">20</parameter></invoke>'
        calls = _extract_xml_tool_calls(xml, KNOWN_TOOLS)
        assert len(calls) == 1
        assert calls[0].input["n"] == 20


# ---------------------------------------------------------------------------
# _repair_tool_calls (schema-aware)
# ---------------------------------------------------------------------------


class TestRepairToolCalls:
    def test_clean_calls_unchanged(self):
        calls = [ToolCall(id="1", name="grep", input={"pattern": "foo"})]
        result = _repair_tool_calls(calls, TOOL_DEFS)
        assert len(result) == 1
        assert result[0].name == "grep"
        assert result[0].input == {"pattern": "foo"}

    def test_repairs_malformed_name(self):
        calls = [
            ToolCall(
                id="1",
                name='grep" pattern="render|Render|RENDER" include_glob="*.py" path="CDE',
                input={},
            )
        ]
        result = _repair_tool_calls(calls, TOOL_DEFS)
        assert len(result) == 1
        assert result[0].name == "grep"
        assert result[0].input["pattern"] == "render|Render|RENDER"
        assert result[0].input["include_glob"] == "*.py"

    def test_filters_garbage_from_tc_input(self):
        """The real production bug: tc.input has params from a different tool."""
        calls = [
            ToolCall(
                id="1",
                name='grep" pattern="[Ii][Dd][Vv]" path="backend/src" max_results=50></invoke>\n<invok',
                input={"name": "some_symbol", "module_path": "backend/src"},
            )
        ]
        result = _repair_tool_calls(calls, TOOL_DEFS)
        assert len(result) == 1
        assert result[0].name == "grep"
        assert result[0].input["pattern"] == "[Ii][Dd][Vv]"
        assert result[0].input["path"] == "backend/src"
        # Garbage params from tc.input must be filtered out
        assert "name" not in result[0].input
        assert "module_path" not in result[0].input

    def test_unquoted_numeric_preserved(self):
        calls = [
            ToolCall(
                id="1",
                name='grep" pattern="test" max_results=20',
                input={},
            )
        ]
        result = _repair_tool_calls(calls, TOOL_DEFS)
        assert result[0].input["max_results"] == 20

    def test_empty_list(self):
        assert _repair_tool_calls([], TOOL_DEFS) == []

    def test_unrepairable_passes_through(self):
        calls = [ToolCall(id="1", name="totally_broken_garbage", input={})]
        result = _repair_tool_calls(calls, TOOL_DEFS)
        assert result[0].name == "totally_broken_garbage"

    def test_preserves_id(self):
        calls = [
            ToolCall(
                id="tooluse_abc123",
                name='find_symbol" name="authenticate"',
                input={},
            )
        ]
        result = _repair_tool_calls(calls, TOOL_DEFS)
        assert result[0].id == "tooluse_abc123"
        assert result[0].name == "find_symbol"
        assert result[0].input["name"] == "authenticate"

    def test_xml_repair_from_garbled_name(self):
        """When garbled name contains complete XML invoke blocks."""
        calls = [
            ToolCall(
                id="1",
                name=(
                    'grep" pattern="x"></invoke>'
                    '<invoke name="module_summary">'
                    '<parameter name="module_path">src</parameter>'
                    "</invoke>"
                ),
                input={},
            )
        ]
        result = _repair_tool_calls(calls, TOOL_DEFS)
        # Should extract the XML invoke for module_summary
        assert any(r.name == "module_summary" for r in result)

    def test_valid_tc_input_kept_when_schema_matches(self):
        """If tc.input has valid params for the repaired tool, keep them."""
        calls = [
            ToolCall(
                id="1",
                name='grep" pattern="render"',
                input={"max_results": 100},  # valid for grep
            )
        ]
        result = _repair_tool_calls(calls, TOOL_DEFS)
        assert result[0].name == "grep"
        assert result[0].input["pattern"] == "render"
        assert result[0].input["max_results"] == 100


# ---------------------------------------------------------------------------
# _extract_tool_calls_from_text
# ---------------------------------------------------------------------------


class TestExtractToolCallsFromText:
    def test_json_format(self):
        text = 'I will search for it: {"name": "grep", "arguments": {"pattern": "auth", "path": "src"}}'
        calls = _extract_tool_calls_from_text(text, KNOWN_TOOLS)
        assert len(calls) == 1
        assert calls[0].name == "grep"
        assert calls[0].input["pattern"] == "auth"

    def test_function_call_format(self):
        text = 'Let me search: grep(pattern="auth", path="src")'
        calls = _extract_tool_calls_from_text(text, KNOWN_TOOLS)
        assert len(calls) == 1
        assert calls[0].name == "grep"
        assert calls[0].input["pattern"] == "auth"

    def test_xml_format_in_text(self):
        text = 'I will search:\n<invoke name="grep"><parameter name="pattern">auth</parameter></invoke>'
        calls = _extract_tool_calls_from_text(text, KNOWN_TOOLS)
        assert len(calls) == 1
        assert calls[0].name == "grep"
        assert calls[0].input["pattern"] == "auth"

    def test_no_tool_calls(self):
        text = "There is no tool call in this text."
        calls = _extract_tool_calls_from_text(text, KNOWN_TOOLS)
        assert calls == []

    def test_empty_text(self):
        assert _extract_tool_calls_from_text("", KNOWN_TOOLS) == []

    def test_empty_tools(self):
        assert _extract_tool_calls_from_text("grep()", set()) == []

    def test_json_with_parameters_key(self):
        text = '{"name": "read_file", "parameters": {"path": "main.py"}}'
        calls = _extract_tool_calls_from_text(text, KNOWN_TOOLS)
        assert len(calls) == 1
        assert calls[0].name == "read_file"
        assert calls[0].input["path"] == "main.py"

    def test_json_with_input_key(self):
        text = '{"name": "find_symbol", "input": {"name": "auth"}}'
        calls = _extract_tool_calls_from_text(text, KNOWN_TOOLS)
        assert len(calls) == 1
        assert calls[0].name == "find_symbol"

    def test_unknown_tool_in_json_ignored(self):
        text = '{"name": "unknown_tool", "arguments": {"x": 1}}'
        calls = _extract_tool_calls_from_text(text, KNOWN_TOOLS)
        assert calls == []

    def test_multiple_json_calls(self):
        text = (
            'First: {"name": "grep", "arguments": {"pattern": "foo"}} '
            'Then: {"name": "read_file", "arguments": {"path": "bar.py"}}'
        )
        calls = _extract_tool_calls_from_text(text, KNOWN_TOOLS)
        assert len(calls) == 2
        names = {c.name for c in calls}
        assert names == {"grep", "read_file"}

    def test_json_preferred_over_xml(self):
        """JSON extraction has higher priority than XML."""
        text = (
            '{"name": "grep", "arguments": {"pattern": "foo"}}\n'
            '<invoke name="read_file"><parameter name="path">x.py</parameter></invoke>'
        )
        calls = _extract_tool_calls_from_text(text, KNOWN_TOOLS)
        # JSON found first → only JSON returned
        assert len(calls) == 1
        assert calls[0].name == "grep"


# ---------------------------------------------------------------------------
# chat_with_tools integration — schema sanitized & repairs wired in
# ---------------------------------------------------------------------------


class TestChatWithToolsIntegration:
    """Test that chat_with_tools sanitizes schemas and repairs tool calls."""

    def _make_provider(self, mock_response):
        """Create a provider with a mocked Bedrock client."""
        mock_boto3 = MagicMock()
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client
        mock_client.converse.return_value = mock_response
        provider = ClaudeBedrockProvider()
        with patch.dict("sys.modules", {"boto3": mock_boto3}):
            provider._get_client()
        provider._client = mock_client
        return provider, mock_client

    SAMPLE_TOOLS = [
        {
            "name": "grep",
            "description": "Search files",
            "input_schema": {
                "title": "GrepParams",
                "type": "object",
                "required": ["pattern"],
                "properties": {
                    "pattern": {"type": "string", "title": "Pattern"},
                    "path": {
                        "anyOf": [{"type": "string"}, {"type": "null"}],
                        "title": "Path",
                        "default": None,
                    },
                },
            },
        },
        {
            "name": "read_file",
            "description": "Read a file",
            "input_schema": {
                "type": "object",
                "required": ["path"],
                "properties": {
                    "path": {"type": "string"},
                },
            },
        },
    ]

    def test_schema_sanitized_before_sending(self):
        """Tool schemas should have anyOf/title stripped before being sent to Bedrock."""
        response = {
            "output": {"message": {"content": [{"text": "ok"}]}},
            "stopReason": "end_turn",
        }
        provider, mock_client = self._make_provider(response)
        provider.chat_with_tools(
            messages=[{"role": "user", "content": [{"text": "test"}]}],
            tools=self.SAMPLE_TOOLS,
        )
        call_args = mock_client.converse.call_args
        tool_config = call_args.kwargs["toolConfig"]
        grep_schema = tool_config["tools"][0]["toolSpec"]["inputSchema"]["json"]

        # Title should be removed
        assert "title" not in grep_schema
        # anyOf should be converted
        path_prop = grep_schema["properties"]["path"]
        assert "anyOf" not in path_prop
        assert path_prop["type"] == "string"
        # Per-property title removed
        assert "title" not in grep_schema["properties"]["pattern"]

    def test_cache_point_added_for_claude_model(self):
        """Claude models should get cachePoint blocks on system + tool defs.

        Bedrock prompt caching cuts per-iter prompt processing latency
        from ~10s to ~1s on cached prefixes; the system prompt and tool
        definitions are static across an agent run, so they're prime
        candidates for caching.
        """
        response = {
            "output": {"message": {"content": [{"text": "ok"}]}},
            "stopReason": "end_turn",
        }
        provider, mock_client = self._make_provider(response)
        # Default model_id is a claude model
        assert "claude" in provider.model_id.lower()
        provider.chat_with_tools(
            messages=[{"role": "user", "content": [{"text": "test"}]}],
            tools=self.SAMPLE_TOOLS,
            system="You are a code review agent.",
        )
        call_args = mock_client.converse.call_args
        tool_config = call_args.kwargs["toolConfig"]
        system_blocks = call_args.kwargs["system"]

        # cachePoint should be the LAST entry in system, so the text block
        # ahead of it gets cached
        assert system_blocks[-1] == {"cachePoint": {"type": "default"}}
        assert system_blocks[0] == {"text": "You are a code review agent."}

        # cachePoint should be the LAST entry in tools, so all tool defs
        # get cached together
        assert tool_config["tools"][-1] == {"cachePoint": {"type": "default"}}
        # Real tool specs come BEFORE the cachePoint
        non_cache_tools = [t for t in tool_config["tools"] if "toolSpec" in t]
        assert len(non_cache_tools) == len(self.SAMPLE_TOOLS)

    def test_cache_point_skipped_for_non_claude_model(self):
        """Non-Claude Bedrock models (Qwen, Nova, DeepSeek) reject cachePoint
        with ValidationException — must NOT add it for them."""
        response = {
            "output": {"message": {"content": [{"text": "ok"}]}},
            "stopReason": "end_turn",
        }
        provider, mock_client = self._make_provider(response)
        provider.model_id = "qwen.qwen3-coder-next"
        provider.chat_with_tools(
            messages=[{"role": "user", "content": [{"text": "test"}]}],
            tools=self.SAMPLE_TOOLS,
            system="You are a code review agent.",
        )
        call_args = mock_client.converse.call_args
        tool_config = call_args.kwargs["toolConfig"]
        system_blocks = call_args.kwargs["system"]

        # No cachePoint should appear for non-Claude models
        assert all("cachePoint" not in b for b in system_blocks)
        assert all("cachePoint" not in t for t in tool_config["tools"])

    def test_malformed_tool_calls_repaired(self):
        """Tool calls with params in name field should be repaired."""
        response = {
            "output": {
                "message": {
                    "content": [
                        {
                            "toolUse": {
                                "toolUseId": "id1",
                                "name": 'grep" pattern="render" path="src"',
                                "input": {},
                            }
                        }
                    ]
                }
            },
            "stopReason": "tool_use",
        }
        provider, _ = self._make_provider(response)
        result = provider.chat_with_tools(
            messages=[{"role": "user", "content": [{"text": "find render"}]}],
            tools=self.SAMPLE_TOOLS,
        )
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].name == "grep"
        assert result.tool_calls[0].input["pattern"] == "render"
        assert result.tool_calls[0].input["path"] == "src"

    def test_garbage_input_filtered_by_schema(self):
        """When name is garbled AND tc.input has wrong-tool params, filter them."""
        response = {
            "output": {
                "message": {
                    "content": [
                        {
                            "toolUse": {
                                "toolUseId": "id1",
                                "name": 'grep" pattern="[Ii]dv" path="backend/src"',
                                "input": {"name": "sym", "module_path": "backend"},
                            }
                        }
                    ]
                }
            },
            "stopReason": "tool_use",
        }
        provider, _ = self._make_provider(response)
        result = provider.chat_with_tools(
            messages=[{"role": "user", "content": [{"text": "find idv"}]}],
            tools=self.SAMPLE_TOOLS,
        )
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].name == "grep"
        assert result.tool_calls[0].input["pattern"] == "[Ii]dv"
        # Garbage from tc.input filtered out (not valid grep params)
        assert "name" not in result.tool_calls[0].input
        assert "module_path" not in result.tool_calls[0].input

    def test_text_based_tool_extraction_fallback(self):
        """When no toolUse blocks but text contains tool calls, extract them."""
        response = {
            "output": {
                "message": {"content": [{"text": 'I will search: {"name": "grep", "arguments": {"pattern": "auth"}}'}]}
            },
            "stopReason": "end_turn",
        }
        provider, _ = self._make_provider(response)
        result = provider.chat_with_tools(
            messages=[{"role": "user", "content": [{"text": "find auth"}]}],
            tools=self.SAMPLE_TOOLS,
        )
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].name == "grep"
        assert result.tool_calls[0].input["pattern"] == "auth"
        assert result.stop_reason == "tool_use"

    def test_normal_tool_call_unchanged(self):
        """Clean tool calls from Bedrock should pass through unchanged."""
        response = {
            "output": {
                "message": {
                    "content": [
                        {"text": "Let me search."},
                        {
                            "toolUse": {
                                "toolUseId": "id1",
                                "name": "grep",
                                "input": {"pattern": "auth"},
                            }
                        },
                    ]
                }
            },
            "stopReason": "tool_use",
            "usage": {
                "inputTokens": 100,
                "outputTokens": 50,
                "totalTokens": 150,
            },
        }
        provider, _ = self._make_provider(response)
        result = provider.chat_with_tools(
            messages=[{"role": "user", "content": [{"text": "find auth"}]}],
            tools=self.SAMPLE_TOOLS,
        )
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].name == "grep"
        assert result.tool_calls[0].input == {"pattern": "auth"}
        assert result.text == "Let me search."
        assert result.usage.input_tokens == 100

    def test_no_text_extraction_when_structured_calls_exist(self):
        """Text extraction should NOT run when structured tool calls exist."""
        response = {
            "output": {
                "message": {
                    "content": [
                        {"text": '{"name": "read_file", "arguments": {"path": "x.py"}}'},
                        {
                            "toolUse": {
                                "toolUseId": "id1",
                                "name": "grep",
                                "input": {"pattern": "test"},
                            }
                        },
                    ]
                }
            },
            "stopReason": "tool_use",
        }
        provider, _ = self._make_provider(response)
        result = provider.chat_with_tools(
            messages=[{"role": "user", "content": [{"text": "search"}]}],
            tools=self.SAMPLE_TOOLS,
        )
        # Only the structured tool call should be present
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].name == "grep"

    def test_original_tool_schemas_not_mutated(self):
        """The caller's tool list should not be modified in place."""
        import copy

        tools_before = copy.deepcopy(self.SAMPLE_TOOLS)
        response = {
            "output": {"message": {"content": [{"text": "ok"}]}},
            "stopReason": "end_turn",
        }
        provider, _ = self._make_provider(response)
        provider.chat_with_tools(
            messages=[{"role": "user", "content": [{"text": "test"}]}],
            tools=self.SAMPLE_TOOLS,
        )
        assert self.SAMPLE_TOOLS == tools_before
