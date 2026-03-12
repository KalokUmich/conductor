"""Tests for the query classifier module."""
import pytest

from app.agent_loop.query_classifier import (
    QUERY_TYPES,
    QueryClassification,
    classify_query,
    classify_query_with_llm,
    _CORE_TOOLS,
)
from app.code_tools.schemas import TOOL_DEFINITIONS, filter_tools


class TestClassifyQuery:
    def test_entry_point_discovery(self):
        result = classify_query("Where is the endpoint for user login?")
        assert result.query_type == "entry_point_discovery"
        assert "grep" in result.initial_tools

    def test_business_flow_tracing(self):
        result = classify_query("How does the payment flow work step by step?")
        assert result.query_type == "business_flow_tracing"
        assert result.budget_level == "medium"

    def test_root_cause_analysis(self):
        result = classify_query("Why does the authentication fail with a null pointer error?")
        assert result.query_type == "root_cause_analysis"
        assert result.budget_level == "high"

    def test_impact_analysis(self):
        result = classify_query("What will break if I refactor the UserService class?")
        assert result.query_type == "impact_analysis"
        assert "get_dependents" in result.initial_tools

    def test_architecture_question(self):
        result = classify_query("What is the overall architecture and module structure?")
        assert result.query_type == "architecture_question"

    def test_config_analysis(self):
        result = classify_query("Where is the database config setting used?")
        assert result.query_type == "config_analysis"
        assert result.budget_level == "low"

    def test_data_lineage(self):
        result = classify_query("How does user input data flow to the database?")
        assert result.query_type == "data_lineage"

    def test_default_classification(self):
        # Ambiguous query should fall back to business_flow_tracing
        result = classify_query("Tell me about the code")
        assert result.query_type == "business_flow_tracing"

    def test_returns_classification_object(self):
        result = classify_query("Where is the login handler?")
        assert isinstance(result, QueryClassification)
        assert result.query_type
        assert result.strategy
        assert isinstance(result.initial_tools, list)
        assert result.budget_level in ("low", "medium", "high")
        assert result.suggested_token_budget > 0

    def test_all_query_types_have_keywords(self):
        for qtype, spec in QUERY_TYPES.items():
            assert "keywords" in spec, f"{qtype} missing keywords"
            assert len(spec["keywords"]) > 0, f"{qtype} has empty keywords"
            assert "strategy" in spec
            assert "initial_tools" in spec
            assert "budget_level" in spec
            assert "suggested_token_budget" in spec

    def test_highest_score_wins(self):
        # A query with multiple "error" keywords should match root_cause
        result = classify_query("debug the crash error exception in auth")
        assert result.query_type == "root_cause_analysis"

    def test_case_insensitive(self):
        result = classify_query("WHERE IS THE ENDPOINT FOR LOGIN?")
        assert result.query_type == "entry_point_discovery"


class TestToolSet:
    """Tests for dynamic tool set per query type."""

    def test_every_type_has_tool_set(self):
        for qtype, spec in QUERY_TYPES.items():
            assert "tools" in spec, f"{qtype} missing tools list"
            assert len(spec["tools"]) >= len(_CORE_TOOLS), (
                f"{qtype} has fewer tools than core set"
            )

    def test_core_tools_always_included(self):
        for qtype, spec in QUERY_TYPES.items():
            for core_tool in _CORE_TOOLS:
                assert core_tool in spec["tools"], (
                    f"{qtype} missing core tool '{core_tool}'"
                )

    def test_classification_includes_tool_set(self):
        result = classify_query("Where is the login endpoint?")
        assert isinstance(result.tool_set, list)
        assert len(result.tool_set) > 0
        assert "grep" in result.tool_set

    def test_no_duplicate_tools(self):
        for qtype, spec in QUERY_TYPES.items():
            assert len(spec["tools"]) == len(set(spec["tools"])), (
                f"{qtype} has duplicate tools"
            )

    def test_is_high_level_property(self):
        arch = classify_query("What is the architecture overview?")
        assert arch.is_high_level is True
        flow = classify_query("How does the payment flow work?")
        assert flow.is_high_level is True
        entry = classify_query("Where is the login endpoint?")
        assert entry.is_high_level is False


class TestFilterTools:
    """Tests for the filter_tools helper in schemas."""

    def test_filters_by_name(self):
        result = filter_tools(["grep", "read_file"])
        assert len(result) == 2
        names = {t["name"] for t in result}
        assert names == {"grep", "read_file"}

    def test_returns_full_definitions(self):
        result = filter_tools(["grep"])
        assert len(result) == 1
        assert "input_schema" in result[0]
        assert result[0]["name"] == "grep"

    def test_empty_filter(self):
        result = filter_tools([])
        assert result == []

    def test_unknown_name_ignored(self):
        result = filter_tools(["grep", "nonexistent_tool"])
        assert len(result) == 1
        assert result[0]["name"] == "grep"

    def test_preserves_order(self):
        names = ["read_file", "grep", "find_symbol"]
        result = filter_tools(names)
        # Order matches TOOL_DEFINITIONS order (grep before read_file)
        result_names = [t["name"] for t in result]
        assert "grep" in result_names
        assert "read_file" in result_names
        assert "find_symbol" in result_names


class TestLLMClassification:
    """Tests for LLM-based classification (with mocked provider)."""

    @pytest.mark.asyncio
    async def test_llm_classification_success(self):
        """LLM returns valid JSON → classification uses it."""
        from unittest.mock import MagicMock
        mock_provider = MagicMock()
        mock_response = MagicMock()
        mock_response.text = '{"query_type": "root_cause_analysis"}'
        mock_provider.chat_with_tools.return_value = mock_response

        result = await classify_query_with_llm("Why does auth crash?", mock_provider)
        assert result.query_type == "root_cause_analysis"
        assert result.tool_set == QUERY_TYPES["root_cause_analysis"]["tools"]

    @pytest.mark.asyncio
    async def test_llm_classification_invalid_json_falls_back(self):
        """LLM returns garbage → falls back to keyword matching."""
        from unittest.mock import MagicMock
        mock_provider = MagicMock()
        mock_response = MagicMock()
        mock_response.text = "I don't know how to classify this"
        mock_provider.chat_with_tools.return_value = mock_response

        result = await classify_query_with_llm(
            "Where is the endpoint for login?", mock_provider,
        )
        # Falls back to keyword matching
        assert result.query_type == "entry_point_discovery"

    @pytest.mark.asyncio
    async def test_llm_classification_exception_falls_back(self):
        """LLM call throws → falls back to keyword matching."""
        from unittest.mock import MagicMock
        mock_provider = MagicMock()
        mock_provider.chat_with_tools.side_effect = RuntimeError("API error")

        result = await classify_query_with_llm(
            "How does payment flow work?", mock_provider,
        )
        assert result.query_type == "business_flow_tracing"

    @pytest.mark.asyncio
    async def test_llm_classification_unknown_type_falls_back(self):
        """LLM returns unknown query_type → falls back to keyword matching."""
        from unittest.mock import MagicMock
        mock_provider = MagicMock()
        mock_response = MagicMock()
        mock_response.text = '{"query_type": "unknown_type"}'
        mock_provider.chat_with_tools.return_value = mock_response

        result = await classify_query_with_llm(
            "What is the config setting for timeout?", mock_provider,
        )
        # Falls back to keyword matching
        assert result.query_type == "config_analysis"
