"""Claude Bedrock provider implementation.

This module provides an AIProvider implementation that connects to Claude
via AWS Bedrock service using the Converse API.

The Converse API is AWS Bedrock's unified API that supports:
- All Claude models (Claude 3, Claude 3.5, Claude 4, etc.)
- Cross-region inference profiles (e.g., us.anthropic.claude-sonnet-4-5-20250929-v1:0)
- Single-region models (e.g., anthropic.claude-3-haiku-20240307-v1:0)
- Non-Claude models (Qwen, Llama, etc.) via Bedrock
- Tool use (function calling) via toolConfig

Usage:
    provider = ClaudeBedrockProvider(
        aws_access_key_id="...",
        aws_secret_access_key="...",
        region_name="us-east-1"
    )
    if provider.health_check():
        summary = provider.summarize_structured(messages)
"""
import copy
import json
import logging
import re
import uuid
from typing import Any, Dict, List, Optional, Set

from .base import AIProvider, ChatMessage, DecisionSummary, TokenUsage, ToolCall, ToolUseResponse
from .pipeline import _strip_markdown_code_block
from .prompts import get_summary_prompt

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Schema sanitization — Bedrock / non-Claude models choke on some JSON Schema
# features that Pydantic v2 generates (anyOf for Optional, $defs, title, etc.)
# ---------------------------------------------------------------------------

def _sanitize_schema(schema: Dict[str, Any]) -> Dict[str, Any]:
    """Clean a Pydantic v2 JSON Schema for maximum Bedrock compatibility.

    Fixes:
      1. ``anyOf: [{type: X}, {type: null}]`` → ``type: X`` (Optional fields)
      2. Remove top-level ``title`` (e.g. "GrepParams") — models don't need it
      3. Remove per-property ``title`` (e.g. "Include Glob") — noise
      4. Remove ``$defs`` / ``$ref`` — inline definitions instead
    """
    schema = copy.deepcopy(schema)

    # Remove top-level noise
    schema.pop("title", None)
    schema.pop("$defs", None)
    schema.pop("definitions", None)

    props = schema.get("properties", {})
    for key, prop in props.items():
        _sanitize_property(prop)

    return schema


def _sanitize_property(prop: Dict[str, Any]) -> None:
    """Sanitize a single property in-place."""
    # Remove per-property title
    prop.pop("title", None)

    # Convert anyOf: [{type: X}, {type: null}] → type: X
    any_of = prop.get("anyOf")
    if isinstance(any_of, list):
        non_null = [t for t in any_of if t.get("type") != "null"]
        if len(non_null) == 1:
            # Merge the non-null type into the property
            real_type = non_null[0]
            prop.pop("anyOf")
            prop.update(real_type)
            # Clean any title from the merged type
            prop.pop("title", None)
        elif len(non_null) == 0:
            # All null — just set type to string as fallback
            prop.pop("anyOf")
            prop["type"] = "string"

    # Recurse into nested objects
    if "properties" in prop:
        for sub_prop in prop["properties"].values():
            _sanitize_property(sub_prop)
    if "items" in prop and isinstance(prop["items"], dict):
        _sanitize_property(prop["items"])


# ---------------------------------------------------------------------------
# Schema-aware tool call repair
#
# Models (especially non-Claude on Bedrock) produce several failure modes:
#   1. Params jammed into the name field: name='grep" pattern="val"'
#   2. XML-style tool calls in name: name='grep" ...></invoke><invok...'
#   3. tc.input contains params from a DIFFERENT tool (garbage)
#   4. Tool calls embedded in text instead of structured blocks
#
# The repair pipeline:
#   a. Build a param_registry {tool_name → valid_param_names} from schemas
#   b. For clean calls (name in known_tools): pass through as-is
#   c. For garbled calls: try multiple extraction strategies, then validate
#      extracted params against the tool's schema
#   d. Fallback: extract tool calls from text content
# ---------------------------------------------------------------------------


def _build_param_registry(
    tool_defs: List[Dict[str, Any]],
) -> Dict[str, Set[str]]:
    """Build a mapping of tool_name → set of valid parameter names.

    Uses the ``properties`` keys from each tool's ``input_schema``.
    """
    registry: Dict[str, Set[str]] = {}
    for tool in tool_defs:
        name = tool.get("name", "")
        schema = tool.get("input_schema", {})
        props = schema.get("properties", {})
        registry[name] = set(props.keys())
    return registry


def _validate_params(
    params: Dict[str, Any],
    tool_name: str,
    registry: Dict[str, Set[str]],
) -> Dict[str, Any]:
    """Keep only params that the tool's schema accepts.

    Unknown keys are silently dropped (they'd cause execution errors).
    """
    valid_keys = registry.get(tool_name)
    if valid_keys is None:
        return params  # unknown tool — can't validate
    filtered = {k: v for k, v in params.items() if k in valid_keys}
    dropped = set(params) - set(filtered)
    if dropped:
        logger.debug(
            "Dropped invalid params for %s: %s", tool_name, dropped,
        )
    return filtered


def _repair_tool_calls(
    tool_calls: List[ToolCall],
    tool_defs: List[Dict[str, Any]],
) -> List[ToolCall]:
    """Repair malformed tool calls using schema-aware validation.

    Args:
        tool_calls: Raw tool calls parsed from the LLM response.
        tool_defs:  Full tool definitions (name + input_schema) for
                    schema validation.

    Returns:
        Repaired tool calls with validated params.
    """
    if not tool_calls:
        return tool_calls

    registry = _build_param_registry(tool_defs)
    known_tools = set(registry.keys())

    repaired: List[ToolCall] = []
    for tc in tool_calls:
        # Clean call — name matches a known tool
        if tc.name in known_tools:
            repaired.append(tc)
            continue

        # --- Garbled name: try multiple extraction strategies ---

        # Strategy 1: XML fragments in name (e.g. '</invoke><invoke name="grep"...')
        xml_calls = _extract_xml_tool_calls(tc.name, known_tools)
        if xml_calls:
            for xc in xml_calls:
                xc_input = _validate_params(xc.input, xc.name, registry)
                logger.warning(
                    "Repaired XML tool call from garbled name: '%s' → name='%s' input=%s",
                    tc.name[:80], xc.name, list(xc_input.keys()),
                )
                repaired.append(ToolCall(id=xc.id or tc.id, name=xc.name, input=xc_input))
            continue

        # Strategy 2: key="value" pairs in name (e.g. 'grep" pattern="val"')
        fixed_name, parsed_params = _parse_malformed_name(tc.name, known_tools)
        if fixed_name:
            # Schema-validate: merge parsed + tc.input, then filter
            merged = {**tc.input, **parsed_params}  # parsed wins on conflict
            final_input = _validate_params(merged, fixed_name, registry)
            logger.warning(
                "Repaired KV tool call: '%s' → name='%s' input=%s",
                tc.name[:80], fixed_name, list(final_input.keys()),
            )
            repaired.append(ToolCall(id=tc.id, name=fixed_name, input=final_input))
            continue

        # Can't repair — pass through (will get "Unknown tool" error)
        logger.warning("Cannot repair malformed tool name: '%s'", tc.name[:100])
        repaired.append(tc)

    return repaired


def _parse_malformed_name(
    raw_name: str,
    known_tools: Set[str],
) -> tuple:
    """Extract tool name + params from a garbled tool name string.

    Handles: ``grep" pattern="value" key2="value2"``

    Returns (tool_name, params_dict) or (None, {}).
    """
    for tool_name in known_tools:
        if raw_name.startswith(tool_name):
            remainder = raw_name[len(tool_name):]
            if not remainder:
                return tool_name, {}
            params = _extract_kv_pairs(remainder)
            if params:
                return tool_name, params

    return None, {}


# Matches both quoted  key="value"  and unquoted  key=123
_KV_QUOTED = re.compile(r'(\w+)\s*=\s*"([^"]*)"?')
_KV_UNQUOTED = re.compile(r'(\w+)\s*=\s*(\d+)(?=[\s>,/]|$)')


def _extract_kv_pairs(text: str) -> Dict[str, Any]:
    """Extract key=value pairs from a string.

    Supports:
      - ``key="value"``   — quoted string
      - ``key=123``        — unquoted integer
    """
    pairs: Dict[str, Any] = {}
    # Quoted values first (higher priority)
    for m in _KV_QUOTED.finditer(text):
        key, val = m.group(1), m.group(2)
        if val.isdigit():
            pairs[key] = int(val)
        else:
            pairs[key] = val
    # Unquoted integers (only if not already captured)
    for m in _KV_UNQUOTED.finditer(text):
        key, val = m.group(1), m.group(2)
        if key not in pairs:
            pairs[key] = int(val)
    return pairs


# ---------------------------------------------------------------------------
# XML tool call extraction — models sometimes produce <invoke> XML fragments
# ---------------------------------------------------------------------------

# <invoke name="grep"><parameter name="pattern">value</parameter></invoke>
_XML_INVOKE = re.compile(
    r'<invoke\s+name\s*=\s*"([^"]+)"[^>]*>'
    r'(.*?)'
    r'</invoke>',
    re.DOTALL,
)
_XML_PARAM = re.compile(
    r'<parameter\s+name\s*=\s*"([^"]+)"[^>]*>'
    r'(.*?)'
    r'</parameter>',
    re.DOTALL,
)
# Also handle attribute-style: <invoke name="grep" pattern="val" path="dir"/>
_XML_ATTR_INVOKE = re.compile(
    r'<invoke\s+name\s*=\s*"([^"]+)"([^>]*)/?>'
)
_XML_ATTR_KV = re.compile(r'(\w+)\s*=\s*"([^"]*)"')


def _extract_xml_tool_calls(
    text: str,
    known_tools: Set[str],
) -> List[ToolCall]:
    """Extract tool calls from XML-format fragments.

    Handles:
      - ``<invoke name="grep"><parameter name="pattern">val</parameter></invoke>``
      - ``<invoke name="grep" pattern="val" path="dir"/>``
    """
    if "<invoke" not in text and "<function_call" not in text:
        return []

    calls: List[ToolCall] = []

    # Pattern 1: <invoke name="..."><parameter name="...">...</parameter></invoke>
    for m in _XML_INVOKE.finditer(text):
        name = m.group(1)
        # The name attribute itself may be garbled (e.g. 'grep" pattern="val"')
        actual_name = name.split('"')[0].strip() if '"' in name else name
        if actual_name not in known_tools:
            continue
        body = m.group(2)
        params: Dict[str, Any] = {}
        for pm in _XML_PARAM.finditer(body):
            pname, pval = pm.group(1), pm.group(2).strip()
            if pval.isdigit():
                params[pname] = int(pval)
            else:
                params[pname] = pval
        calls.append(ToolCall(
            id=f"xml_{uuid.uuid4().hex[:8]}",
            name=actual_name,
            input=params,
        ))

    # Pattern 2: <invoke name="grep" pattern="val" path="dir"/>
    if not calls:
        for m in _XML_ATTR_INVOKE.finditer(text):
            name = m.group(1)
            actual_name = name.split('"')[0].strip() if '"' in name else name
            if actual_name not in known_tools:
                continue
            attrs_str = m.group(2)
            params = {}
            for am in _XML_ATTR_KV.finditer(attrs_str):
                k, v = am.group(1), am.group(2)
                if k == "name":
                    continue  # skip the name attribute itself
                if v.isdigit():
                    params[k] = int(v)
                else:
                    params[k] = v
            if params:
                calls.append(ToolCall(
                    id=f"xml_{uuid.uuid4().hex[:8]}",
                    name=actual_name,
                    input=params,
                ))

    return calls


# ---------------------------------------------------------------------------
# Text-based tool call extraction — for models that put tool calls in text
# ---------------------------------------------------------------------------

def _extract_tool_calls_from_text(
    text: str,
    known_tools: Set[str],
) -> List[ToolCall]:
    """Extract tool calls from text when the model doesn't use structured
    toolUse blocks (common with non-Claude Bedrock models).

    Strategies (tried in order):
      1. JSON objects: {"name": "grep", "arguments": {...}}
      2. XML fragments: <invoke name="grep"><parameter ...>...</parameter></invoke>
      3. Function-call: grep(pattern="...", path="...")
    """
    if not text or not known_tools:
        return []

    calls: List[ToolCall] = []

    # Strategy 1: JSON objects with "name" and "arguments"/"parameters"/"input"
    for m in re.finditer(r'\{', text):
        start = m.start()
        depth = 0
        end = start
        for i in range(start, len(text)):
            if text[i] == '{':
                depth += 1
            elif text[i] == '}':
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if depth != 0:
            continue
        candidate = text[start:end]
        try:
            obj = json.loads(candidate)
            name = obj.get("name", "")
            if name in known_tools:
                params = obj.get("arguments") or obj.get("parameters") or obj.get("input") or {}
                calls.append(ToolCall(
                    id=f"text_{uuid.uuid4().hex[:8]}",
                    name=name,
                    input=params if isinstance(params, dict) else {},
                ))
        except (json.JSONDecodeError, AttributeError, TypeError):
            continue

    if calls:
        return calls

    # Strategy 2: XML fragments in text body
    xml_calls = _extract_xml_tool_calls(text, known_tools)
    if xml_calls:
        return xml_calls

    # Strategy 3: function_name(key="value", key2="value2") pattern
    tool_names_pattern = "|".join(re.escape(t) for t in known_tools)
    fn_pattern = re.compile(
        rf'(?:^|\s)({tool_names_pattern})\s*\(([^)]*)\)',
        re.MULTILINE,
    )
    for m in fn_pattern.finditer(text):
        name = m.group(1)
        args_str = m.group(2)
        params = _extract_kv_pairs(args_str)
        if params:
            calls.append(ToolCall(
                id=f"text_{uuid.uuid4().hex[:8]}",
                name=name,
                input=params,
            ))

    return calls


class ClaudeBedrockProvider(AIProvider):
    """AIProvider implementation using Claude via AWS Bedrock Converse API.

    This provider connects to Claude through AWS Bedrock using the Converse API,
    which is the recommended unified API for all Bedrock models. It supports both
    single-region models and cross-region inference profiles.

    Attributes:
        aws_access_key_id: AWS access key ID.
        aws_secret_access_key: AWS secret access key.
        region_name: AWS region for Bedrock service.
        model_id: Bedrock model ID or inference profile ID for Claude.
    """

    # Use cross-region inference profile for Claude Sonnet 4.5
    # Format: {region}.{model_id} for inference profiles
    DEFAULT_MODEL_ID = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
    DEFAULT_REGION = "us-east-1"

    def __init__(
        self,
        aws_access_key_id: Optional[str] = None,
        aws_secret_access_key: Optional[str] = None,
        aws_session_token: Optional[str] = None,
        region_name: Optional[str] = None,
        model_id: Optional[str] = None,
    ) -> None:
        """Initialize the Claude Bedrock provider.

        Args:
            aws_access_key_id: AWS access key ID. If None, uses default credential chain.
            aws_secret_access_key: AWS secret access key.
            aws_session_token: Optional AWS session token for temporary credentials.
            region_name: AWS region for Bedrock. Defaults to us-east-1.
            model_id: Bedrock model ID. Defaults to Claude 3 Sonnet.
        """
        self.aws_access_key_id = aws_access_key_id
        self.aws_secret_access_key = aws_secret_access_key
        self.aws_session_token = aws_session_token
        self.region_name = region_name or self.DEFAULT_REGION
        self.model_id = model_id or self.DEFAULT_MODEL_ID
        self._client: Optional[object] = None

    def _get_client(self) -> object:
        """Get or create the Bedrock runtime client.

        Returns:
            Boto3 Bedrock runtime client.

        Raises:
            ImportError: If boto3 package is not installed.
        """
        if self._client is None:
            try:
                import boto3
                kwargs = {"region_name": self.region_name}
                if self.aws_access_key_id and self.aws_secret_access_key:
                    kwargs["aws_access_key_id"] = self.aws_access_key_id
                    kwargs["aws_secret_access_key"] = self.aws_secret_access_key
                if self.aws_session_token:
                    kwargs["aws_session_token"] = self.aws_session_token
                self._client = boto3.client("bedrock-runtime", **kwargs)
            except ImportError:
                raise ImportError(
                    "boto3 package is required for ClaudeBedrockProvider. "
                    "Install it with: pip install boto3"
                )
        return self._client

    def health_check(self) -> bool:
        """Check if Claude via Bedrock is accessible.

        Attempts a minimal API call using the Converse API to verify connectivity.
        The Converse API supports both single-region models and cross-region
        inference profiles.

        Returns:
            bool: True if Bedrock is accessible, False otherwise.
        """
        try:
            client = self._get_client()
            # Use Converse API for health check - works with all model types
            response = client.converse(
                modelId=self.model_id,
                messages=[
                    {
                        "role": "user",
                        "content": [{"text": "hi"}]
                    }
                ],
                inferenceConfig={
                    "maxTokens": 1,
                }
            )
            return True
        except Exception as e:
            logger.warning(f"Claude Bedrock health check failed: {e}")
            return False

    def summarize(self, messages: List[str]) -> str:
        """Generate a summary of the provided messages using Claude via Bedrock.

        Uses the Converse API for compatibility with all model types.

        Args:
            messages: List of message strings to summarize.

        Returns:
            str: A concise summary of the messages.

        Raises:
            Exception: If the API call fails.
        """
        if not messages:
            return ""

        client = self._get_client()
        combined_messages = "\n".join(messages)

        prompt = (
            "Please provide a concise summary of the following messages:\n\n"
            f"{combined_messages}"
        )

        response = client.converse(
            modelId=self.model_id,
            messages=[
                {
                    "role": "user",
                    "content": [{"text": prompt}]
                }
            ],
            inferenceConfig={
                "maxTokens": 1024,
            }
        )

        return response["output"]["message"]["content"][0]["text"]

    def summarize_structured(self, messages: List[ChatMessage]) -> DecisionSummary:
        """Generate a structured decision summary from chat messages.

        Uses the Converse API for compatibility with all model types.

        Args:
            messages: List of ChatMessage objects to summarize.

        Returns:
            DecisionSummary: A structured summary with topic, problem,
                solution, and other decision-related fields.

        Raises:
            Exception: If the API call fails or JSON parsing fails.
        """
        if not messages:
            return DecisionSummary()

        client = self._get_client()

        # Generate prompt using shared template
        prompt = get_summary_prompt(messages)

        response = client.converse(
            modelId=self.model_id,
            messages=[
                {
                    "role": "user",
                    "content": [{"text": prompt}]
                }
            ],
            inferenceConfig={
                "maxTokens": 2048,
            }
        )

        response_text = response["output"]["message"]["content"][0]["text"].strip()
        response_text = _strip_markdown_code_block(response_text)

        # Parse JSON response
        try:
            data = json.loads(response_text)
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse JSON response: {response_text}")
            raise ValueError(f"Invalid JSON response from AI: {e}")

        # Validate and extract fields with defaults
        return DecisionSummary(
            type="decision_summary",
            topic=data.get("topic", ""),
            problem_statement=data.get("problem_statement", ""),
            proposed_solution=data.get("proposed_solution", ""),
            requires_code_change=data.get("requires_code_change", False),
            affected_components=data.get("affected_components", []),
            risk_level=data.get("risk_level", "low"),
            next_steps=data.get("next_steps", []),
        )

    def call_model(
        self,
        prompt: str,
        max_tokens: int = 2048,
        system: str | None = None,
    ) -> str:
        """Call the Claude model via Bedrock with a raw prompt.

        Uses the Converse API for compatibility with all model types.

        Args:
            prompt:     The user-turn prompt to send to the model.
            max_tokens: Maximum tokens in the response.
            system:     Optional system instruction (maps to the Converse
                        ``system`` parameter as a text block).

        Returns:
            str: The model's response text.

        Raises:
            Exception: If the API call fails.
        """
        client = self._get_client()

        kwargs: dict = {
            "modelId": self.model_id,
            "messages": [
                {
                    "role": "user",
                    "content": [{"text": prompt}],
                }
            ],
            "inferenceConfig": {"maxTokens": max_tokens},
        }
        if system:
            kwargs["system"] = [{"text": system}]

        response = client.converse(**kwargs)
        return response["output"]["message"]["content"][0]["text"].strip()

    def chat_with_tools(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        max_tokens: int = 4096,
        system: str | None = None,
    ) -> ToolUseResponse:
        """Send messages with tool definitions via the Bedrock Converse API.

        The Converse API natively supports toolConfig for function calling.
        Tool definitions use the Bedrock toolSpec format.
        """
        client = self._get_client()

        # Build known_tools set for text extraction fallback
        known_tools: Set[str] = {t["name"] for t in tools}

        # Convert tool definitions to Bedrock toolConfig format
        # Apply _sanitize_schema to prevent Pydantic v2 anyOf/title issues
        tool_specs = []
        for tool in tools:
            raw_schema = tool.get("input_schema", {})
            spec = {
                "toolSpec": {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "inputSchema": {
                        "json": _sanitize_schema(raw_schema),
                    },
                }
            }
            tool_specs.append(spec)

        kwargs: dict = {
            "modelId": self.model_id,
            "messages": messages,
            "inferenceConfig": {"maxTokens": max_tokens},
        }
        if tool_specs:
            kwargs["toolConfig"] = {"tools": tool_specs}
        if system:
            kwargs["system"] = [{"text": system}]

        response = client.converse(**kwargs)

        # Parse the response
        output = response.get("output", {}).get("message", {})
        content_blocks = output.get("content", [])
        stop_reason = response.get("stopReason", "end_turn")

        text_parts = []
        tool_calls = []

        for block in content_blocks:
            if "text" in block:
                text_parts.append(block["text"])
            elif "toolUse" in block:
                tu = block["toolUse"]
                tool_calls.append(ToolCall(
                    id=tu["toolUseId"],
                    name=tu["name"],
                    input=tu.get("input", {}),
                ))

        # Repair malformed tool calls (schema-aware, multi-strategy)
        tool_calls = _repair_tool_calls(tool_calls, tools)

        # Fallback: if no structured tool calls but text contains tool
        # call patterns, extract them from text
        if not tool_calls and text_parts:
            full_text = "\n".join(text_parts)
            extracted = _extract_tool_calls_from_text(full_text, known_tools)
            if extracted:
                logger.info(
                    "Extracted %d tool call(s) from text (no structured toolUse blocks)",
                    len(extracted),
                )
                tool_calls = extracted
                # Override stop_reason since we found tool calls
                stop_reason = "tool_use"

        # Extract token usage from Bedrock Converse response
        usage = None
        raw_usage = response.get("usage")
        if raw_usage:
            usage = TokenUsage(
                input_tokens=raw_usage.get("inputTokens", 0),
                output_tokens=raw_usage.get("outputTokens", 0),
                total_tokens=raw_usage.get("totalTokens", 0),
                cache_read_input_tokens=raw_usage.get("cacheReadInputTokens", 0),
                cache_write_input_tokens=raw_usage.get("cacheWriteInputTokens", 0),
            )

        return ToolUseResponse(
            text="\n".join(text_parts),
            tool_calls=tool_calls,
            stop_reason=stop_reason,
            raw=response,
            usage=usage,
        )
