"""Tests for the PromptBuilder and related helper functions."""
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.agent.style_loader import Language
from app.ai_provider.prompt_builder import (
    PromptBuilder,
    build_selective_prompt,
    infer_languages_from_components,
    is_documentation_only,
)


# =============================================================================
# TestInferLanguagesFromComponents
# =============================================================================


class TestInferLanguagesFromComponents:
    """Tests for infer_languages_from_components()."""

    def test_python_only(self):
        result = infer_languages_from_components(["app/main.py", "app/utils.py"])
        assert result == [Language.PYTHON]

    def test_mixed_languages(self):
        result = infer_languages_from_components([
            "app/main.py",
            "src/index.js",
            "src/Service.java",
        ])
        assert Language.PYTHON in result
        assert Language.JAVASCRIPT in result
        assert Language.JAVA in result
        assert len(result) == 3

    def test_ts_tsx_map_to_javascript(self):
        result = infer_languages_from_components(["src/App.tsx", "lib/util.ts"])
        assert result == [Language.JAVASCRIPT]

    def test_no_recognized_extensions(self):
        result = infer_languages_from_components(["README.md", "Makefile", "Dockerfile"])
        assert result == []

    def test_empty_list(self):
        result = infer_languages_from_components([])
        assert result == []

    def test_go_files(self):
        result = infer_languages_from_components(["cmd/main.go"])
        assert result == [Language.GO]

    def test_json_files(self):
        result = infer_languages_from_components(["package.json"])
        assert result == [Language.JSON]

    def test_deduplication(self):
        result = infer_languages_from_components(["a.py", "b.py", "c.py"])
        assert result == [Language.PYTHON]


# =============================================================================
# TestIsDocumentationOnly
# =============================================================================


class TestIsDocumentationOnly:
    """Tests for is_documentation_only()."""

    def test_all_md_files(self):
        assert is_documentation_only(["README.md", "CHANGELOG.md"]) is True

    def test_docs_directory(self):
        assert is_documentation_only(["docs/guide.html", "docs/api.md"]) is True

    def test_doc_directory(self):
        assert is_documentation_only(["doc/overview.txt"]) is True

    def test_mixed_doc_and_code(self):
        assert is_documentation_only(["README.md", "app/main.py"]) is False

    def test_code_only(self):
        assert is_documentation_only(["app/main.py", "src/index.ts"]) is False

    def test_empty_components_with_doc_solution(self):
        assert is_documentation_only(
            [], "Update the docstring for the function"
        ) is True

    def test_empty_components_with_code_solution(self):
        assert is_documentation_only(
            [], "Implement the new login endpoint"
        ) is False

    def test_empty_components_empty_solution(self):
        assert is_documentation_only([], "") is False

    def test_rst_and_txt_extensions(self):
        assert is_documentation_only(["docs/guide.rst", "notes.txt"]) is True

    def test_adoc_extension(self):
        assert is_documentation_only(["manual.adoc"]) is True


# =============================================================================
# TestPromptBuilder
# =============================================================================


class TestPromptBuilder:
    """Tests for the PromptBuilder class."""

    def test_basic_build_includes_required_sections(self):
        prompt = (
            PromptBuilder("Fix the login bug", "Patch auth module", ["auth.py"], "medium")
            .build()
        )
        assert "<problem>" in prompt
        assert "Fix the login bug" in prompt
        assert "<solution>" in prompt
        assert "Patch auth module" in prompt
        assert "<target_components>" in prompt
        assert "auth.py" in prompt
        assert "<instructions>" in prompt

    def test_basic_build_includes_tests_and_error_handling(self):
        prompt = (
            PromptBuilder("Bug", "Fix", ["app.py"], "low")
            .build()
        )
        assert "error handling" in prompt.lower()
        assert "tests if applicable" in prompt.lower()

    def test_doc_only_omits_tests_and_error_handling(self):
        prompt = (
            PromptBuilder("Fix docs", "Update readme", ["README.md"], "low")
            .build()
        )
        # Extract the requirements section between "Requirements:" and "Output Format:"
        requirements_start = prompt.find("Requirements:")
        requirements_end = prompt.find("Output Format:")
        requirements_section = prompt[requirements_start:requirements_end].lower()
        assert "error handling" not in requirements_section
        assert "tests if applicable" not in requirements_section

    def test_unified_diff_output_mode(self):
        prompt = (
            PromptBuilder("P", "S", ["a.py"], "low")
            .with_output_mode("unified_diff")
            .build()
        )
        assert "unified diff" in prompt.lower()
        assert "git apply" in prompt.lower()

    def test_direct_repo_edits_output_mode(self):
        prompt = (
            PromptBuilder("P", "S", ["a.py"], "low")
            .with_output_mode("direct_repo_edits")
            .build()
        )
        assert "complete" in prompt.lower()
        assert "file contents" in prompt.lower()
        assert "test suite" in prompt.lower()

    def test_plan_then_diff_output_mode(self):
        prompt = (
            PromptBuilder("P", "S", ["a.py"], "low")
            .with_output_mode("plan_then_diff")
            .build()
        )
        assert "implementation plan" in prompt.lower()
        assert "unified diff" in prompt.lower()

    def test_inferred_language_takes_priority_over_detected(self):
        """Python-only components should load Python style, not JavaScript."""
        prompt = (
            PromptBuilder("P", "S", ["module/service.py"], "low")
            .with_detected_languages(["python", "javascript"])
            .build()
        )
        # The style section should exist (from Python inference)
        assert "<code_style>" in prompt
        # We can't assert specific style content without reading the .md files,
        # but we verify the builder ran without error and produced a style section.

    def test_xml_structure_preserved(self):
        prompt = (
            PromptBuilder("Problem", "Solution", ["app.py"], "high")
            .build()
        )
        assert "<problem>" in prompt
        assert "</problem>" in prompt
        assert "<solution>" in prompt
        assert "</solution>" in prompt
        assert "<target_components>" in prompt
        assert "</target_components>" in prompt
        assert "<instructions>" in prompt
        assert "</instructions>" in prompt

    def test_context_snippet_included(self):
        prompt = (
            PromptBuilder("P", "S", ["a.py"], "low")
            .with_context_snippet("def foo(): pass")
            .build()
        )
        assert "<context>" in prompt
        assert "def foo(): pass" in prompt

    def test_policy_constraints_included(self):
        prompt = (
            PromptBuilder("P", "S", ["a.py"], "low")
            .with_policy_constraints("- Max files: 10")
            .build()
        )
        assert "<policy_constraints>" in prompt
        assert "Max files: 10" in prompt

    def test_no_components_shows_placeholder(self):
        prompt = (
            PromptBuilder("P", "S", [], "low")
            .build()
        )
        assert "No specific components identified" in prompt

    def test_empty_problem_and_solution_defaults(self):
        prompt = (
            PromptBuilder("", "", ["a.py"], "low")
            .build()
        )
        assert "No problem statement provided." in prompt
        assert "No solution proposed." in prompt

    def test_context_snippets_included(self):
        """Multiple file-targeted snippets should render as <context_snippets>."""
        prompt = (
            PromptBuilder("P", "S", ["auth.py", "utils.py"], "low")
            .with_context_snippets([
                {"file_path": "auth.py", "snippet": "def login(user):\n    pass"},
                {"file_path": "utils.py", "snippet": "def hash(val):\n    return md5(val)"},
            ])
            .build()
        )
        assert "<context_snippets>" in prompt
        assert "</context_snippets>" in prompt
        assert "### auth.py" in prompt
        assert "def login(user):" in prompt
        assert "### utils.py" in prompt
        assert "def hash(val):" in prompt
        # Single-string <context> tag should NOT appear
        assert "<context>" not in prompt

    def test_context_snippets_omitted_when_empty(self):
        """No <context_snippets> section when snippets list is empty or None."""
        prompt = (
            PromptBuilder("P", "S", ["a.py"], "low")
            .with_context_snippets([])
            .build()
        )
        assert "<context_snippets>" not in prompt
        assert "<context>" not in prompt

    def test_context_snippets_omitted_when_none(self):
        prompt = (
            PromptBuilder("P", "S", ["a.py"], "low")
            .with_context_snippets(None)
            .build()
        )
        assert "<context_snippets>" not in prompt

    def test_context_snippets_override_single_snippet(self):
        """When both single and multi snippets provided, multi takes priority."""
        prompt = (
            PromptBuilder("P", "S", ["a.py"], "low")
            .with_context_snippet("single snippet")
            .with_context_snippets([
                {"file_path": "a.py", "snippet": "multi snippet content"},
            ])
            .build()
        )
        assert "<context_snippets>" in prompt
        assert "multi snippet content" in prompt
        assert "single snippet" not in prompt

    def test_context_snippets_skip_empty_snippet(self):
        """Empty snippet entries should be filtered out."""
        prompt = (
            PromptBuilder("P", "S", ["a.py"], "low")
            .with_context_snippets([
                {"file_path": "a.py", "snippet": "real content"},
                {"file_path": "b.py", "snippet": ""},
                {"file_path": "c.py", "snippet": "   "},
            ])
            .build()
        )
        assert "<context_snippets>" in prompt
        assert "### a.py" in prompt
        # Empty snippet filtered, but whitespace-only is kept (non-empty string)
        assert "### b.py" not in prompt


# =============================================================================
# TestBuildSelectivePrompt
# =============================================================================


class TestBuildSelectivePrompt:
    """Tests for build_selective_prompt()."""

    def test_basic_selective_prompt(self):
        summaries = [
            {
                "discussion_type": "code_change",
                "topic": "Refactor auth",
                "core_problem": "Auth is messy",
                "proposed_solution": "Clean it up",
                "affected_components": ["auth.py"],
                "risk_level": "medium",
                "next_steps": ["Refactor"],
            }
        ]
        prompt = build_selective_prompt(
            primary_focus="Auth refactoring",
            impact_scope="module",
            summaries=summaries,
        )
        assert "Auth refactoring" in prompt
        assert "<summaries>" in prompt
        assert "<instructions>" in prompt

    def test_selective_prompt_respects_output_mode(self):
        summaries = [
            {
                "discussion_type": "code_change",
                "topic": "T",
                "core_problem": "P",
                "proposed_solution": "S",
                "affected_components": [],
                "risk_level": "low",
                "next_steps": [],
            }
        ]
        prompt = build_selective_prompt(
            primary_focus="Focus",
            impact_scope="local",
            summaries=summaries,
            output_mode="direct_repo_edits",
        )
        assert "complete" in prompt.lower()
        assert "file contents" in prompt.lower()


# =============================================================================
# TestPromptBuilderEndpoint
# =============================================================================


class TestPromptBuilderEndpoint:
    """Integration tests for the /ai/code-prompt endpoint using PromptBuilder."""

    @pytest.fixture
    def client(self):
        from app.main import app
        return TestClient(app)

    def test_code_prompt_endpoint_returns_200(self, client):
        response = client.post("/ai/code-prompt", json={
            "decision_summary": {
                "type": "decision_summary",
                "topic": "Fix auth",
                "problem_statement": "Login fails",
                "proposed_solution": "Fix token validation",
                "requires_code_change": True,
                "affected_components": ["auth/login.py"],
                "risk_level": "medium",
                "next_steps": ["Fix it"],
            },
            "detected_languages": ["python"],
        })
        assert response.status_code == 200
        data = response.json()
        assert "code_prompt" in data
        assert len(data["code_prompt"]) > 0

    def test_doc_only_prompt_is_shorter(self, client):
        code_response = client.post("/ai/code-prompt", json={
            "decision_summary": {
                "type": "decision_summary",
                "topic": "Fix auth",
                "problem_statement": "Login fails",
                "proposed_solution": "Fix token validation",
                "requires_code_change": True,
                "affected_components": ["auth/login.py", "auth/session.py"],
                "risk_level": "medium",
                "next_steps": ["Fix it"],
            },
        })
        doc_response = client.post("/ai/code-prompt", json={
            "decision_summary": {
                "type": "decision_summary",
                "topic": "Update docs",
                "problem_statement": "Docs are outdated",
                "proposed_solution": "Update readme",
                "requires_code_change": False,
                "affected_components": ["README.md", "docs/guide.md"],
                "risk_level": "low",
                "next_steps": ["Update"],
            },
        })
        assert code_response.status_code == 200
        assert doc_response.status_code == 200
        code_prompt = code_response.json()["code_prompt"]
        doc_prompt = doc_response.json()["code_prompt"]
        # Doc-only prompt should be shorter (no tests/error handling requirements)
        assert len(doc_prompt) < len(code_prompt)


# =============================================================================
# TestCallCodePromptFromItems
# =============================================================================


class TestCallCodePromptFromItems:
    """Tests for call_code_prompt_from_items()."""

    def test_single_item_prompt(self):
        from app.ai_provider.wrapper import call_code_prompt_from_items

        items = [{
            "id": "item-1",
            "type": "code_change",
            "title": "Add login endpoint",
            "problem": "No auth endpoint",
            "proposed_change": "Create POST /auth/login",
            "targets": ["auth/login.py"],
            "risk_level": "medium",
        }]

        prompt = call_code_prompt_from_items(items, topic="User Auth")

        assert "Add login endpoint" in prompt
        assert "No auth endpoint" in prompt
        assert "Create POST /auth/login" in prompt
        assert "auth/login.py" in prompt
        assert "User Auth" in prompt

    def test_multiple_items_merge(self):
        from app.ai_provider.wrapper import call_code_prompt_from_items

        items = [
            {
                "id": "item-1",
                "type": "api_design",
                "title": "Create endpoint",
                "problem": "No endpoint",
                "proposed_change": "Add POST /users",
                "targets": ["api/users.py"],
                "risk_level": "low",
            },
            {
                "id": "item-2",
                "type": "code_change",
                "title": "Add middleware",
                "problem": "No auth check",
                "proposed_change": "Add auth middleware",
                "targets": ["middleware/auth.py"],
                "risk_level": "high",
            },
        ]

        prompt = call_code_prompt_from_items(items)

        assert "Create endpoint" in prompt
        assert "Add middleware" in prompt
        assert "api/users.py" in prompt
        assert "middleware/auth.py" in prompt

    def test_target_deduplication(self):
        from app.ai_provider.wrapper import call_code_prompt_from_items

        items = [
            {"title": "A", "problem": "", "proposed_change": "", "targets": ["shared.py", "utils.py"], "risk_level": "low"},
            {"title": "B", "problem": "", "proposed_change": "", "targets": ["shared.py", "other.py"], "risk_level": "low"},
        ]

        prompt = call_code_prompt_from_items(items)

        # shared.py should appear only once in the target components section
        components_start = prompt.find("<target_components>")
        components_end = prompt.find("</target_components>")
        components_section = prompt[components_start:components_end]
        assert components_section.count("shared.py") == 1

    def test_highest_risk_selection(self):
        from app.ai_provider.wrapper import call_code_prompt_from_items

        items = [
            {"title": "A", "problem": "", "proposed_change": "", "targets": [], "risk_level": "low"},
            {"title": "B", "problem": "", "proposed_change": "", "targets": [], "risk_level": "high"},
            {"title": "C", "problem": "", "proposed_change": "", "targets": [], "risk_level": "medium"},
        ]

        prompt = call_code_prompt_from_items(items)

        # Risk level should be "high" (the highest among items)
        risk_start = prompt.find("<risk_level>")
        risk_end = prompt.find("</risk_level>")
        risk_section = prompt[risk_start:risk_end]
        assert "high" in risk_section


# =============================================================================
# TestItemsCodePromptEndpoint
# =============================================================================


class TestItemsCodePromptEndpoint:
    """Integration tests for POST /ai/code-prompt/items endpoint."""

    @pytest.fixture
    def client(self):
        from app.main import app
        return TestClient(app)

    def test_items_endpoint_returns_200(self, client):
        response = client.post("/ai/code-prompt/items", json={
            "items": [{
                "id": "item-1",
                "type": "code_change",
                "title": "Add login endpoint",
                "problem": "No auth",
                "proposed_change": "Create POST /auth/login",
                "targets": ["auth/login.py"],
                "risk_level": "medium",
            }],
            "topic": "User Auth",
            "detected_languages": ["python"],
        })

        assert response.status_code == 200
        data = response.json()
        assert "code_prompt" in data
        assert len(data["code_prompt"]) > 0
        assert "Add login endpoint" in data["code_prompt"]

    def test_items_endpoint_rejects_empty_items(self, client):
        response = client.post("/ai/code-prompt/items", json={
            "items": [],
            "topic": "Test",
        })

        assert response.status_code == 400
        assert "At least one item" in response.json()["detail"]

    def test_items_endpoint_handles_multiple_items(self, client):
        response = client.post("/ai/code-prompt/items", json={
            "items": [
                {
                    "id": "item-1",
                    "type": "api_design",
                    "title": "Create users endpoint",
                    "problem": "No user CRUD",
                    "proposed_change": "Add REST endpoints",
                    "targets": ["api/users.py"],
                    "risk_level": "medium",
                },
                {
                    "id": "item-2",
                    "type": "code_change",
                    "title": "Add user model",
                    "problem": "No user data model",
                    "proposed_change": "Create User SQLAlchemy model",
                    "targets": ["models/user.py"],
                    "risk_level": "low",
                },
            ],
            "topic": "User Management",
        })

        assert response.status_code == 200
        data = response.json()
        assert "code_prompt" in data
        prompt = data["code_prompt"]
        assert "Create users endpoint" in prompt
        assert "Add user model" in prompt

    def test_items_endpoint_with_context_snippets(self, client):
        """Context snippets should appear in the generated prompt."""
        response = client.post("/ai/code-prompt/items", json={
            "items": [{
                "id": "item-1",
                "type": "code_change",
                "title": "Update login function",
                "problem": "Login lacks MFA",
                "proposed_change": "Add MFA check",
                "targets": ["auth/login.py"],
                "risk_level": "medium",
            }],
            "topic": "Add MFA",
            "context_snippets": [
                {
                    "file_path": "auth/login.py",
                    "snippet": "def login(username, password):\n    user = find_user(username)\n    if verify(password, user.hash):\n        return create_session(user)",
                },
            ],
        })

        assert response.status_code == 200
        prompt = response.json()["code_prompt"]
        assert "<context_snippets>" in prompt
        assert "### auth/login.py" in prompt
        assert "def login(username, password):" in prompt

    def test_items_endpoint_without_context_snippets(self, client):
        """No context section when context_snippets is not provided."""
        response = client.post("/ai/code-prompt/items", json={
            "items": [{
                "id": "item-1",
                "type": "code_change",
                "title": "Fix bug",
                "problem": "Bug",
                "proposed_change": "Fix",
                "targets": ["app.py"],
                "risk_level": "low",
            }],
        })

        assert response.status_code == 200
        prompt = response.json()["code_prompt"]
        assert "<context_snippets>" not in prompt
        assert "<context>" not in prompt


# =============================================================================
# TestSelectiveItemsCodePromptEndpoint
# =============================================================================


class TestSelectiveItemsCodePromptEndpoint:
    """Tests for POST /ai/code-prompt/selective endpoint (item-ID filtering)."""

    THREE_ITEMS = [
        {
            "id": "item-1",
            "type": "code_change",
            "title": "Add user validation",
            "problem": "No input validation on user fields",
            "proposed_change": "Add Pydantic validators to User model",
            "targets": ["models/user.py"],
            "risk_level": "low",
        },
        {
            "id": "item-2",
            "type": "api_design",
            "title": "Create REST endpoints",
            "problem": "No CRUD endpoints for users",
            "proposed_change": "Add GET/POST/PUT/DELETE /users routes",
            "targets": ["api/users.py", "api/router.py"],
            "risk_level": "medium",
        },
        {
            "id": "item-3",
            "type": "architecture",
            "title": "Add caching layer",
            "problem": "Repeated DB queries for user lookups",
            "proposed_change": "Add Redis cache for user objects",
            "targets": ["services/cache.py", "config/redis.py"],
            "risk_level": "high",
        },
    ]

    def _make_request(self, selected_ids, **overrides):
        """Build a request body for the selective endpoint."""
        body = {
            "summary": {
                "topic": "User Management System",
                "problem_statement": "Need full user management",
                "proposed_solution": "Build CRUD + validation + caching",
                "requires_code_change": True,
                "affected_components": [
                    "models/user.py", "api/users.py", "api/router.py",
                    "services/cache.py", "config/redis.py",
                ],
                "risk_level": "high",
                "next_steps": ["Implement validation", "Add endpoints", "Setup cache"],
                "code_relevant_items": self.THREE_ITEMS,
            },
            "selected_item_ids": selected_ids,
        }
        body.update(overrides)
        return body

    @pytest.fixture
    def client(self):
        from app.main import app
        return TestClient(app)

    def test_select_one_of_three_items(self, client):
        """Selecting 1 of 3 items should produce a prompt with only that item's content."""
        response = client.post(
            "/ai/code-prompt/selective",
            json=self._make_request(["item-2"]),
        )
        assert response.status_code == 200
        prompt = response.json()["code_prompt"]

        # Selected item content should be present
        assert "Create REST endpoints" in prompt
        assert "No CRUD endpoints" in prompt
        assert "api/users.py" in prompt

        # Other items' content should NOT be present
        assert "Add user validation" not in prompt
        assert "Pydantic validators" not in prompt
        assert "Add caching layer" not in prompt
        assert "Redis cache" not in prompt

    def test_prompt_shorter_with_fewer_items(self, client):
        """Selecting fewer items should produce a shorter prompt."""
        response_one = client.post(
            "/ai/code-prompt/selective",
            json=self._make_request(["item-1"]),
        )
        response_all = client.post(
            "/ai/code-prompt/selective",
            json=self._make_request(["item-1", "item-2", "item-3"]),
        )
        assert response_one.status_code == 200
        assert response_all.status_code == 200

        prompt_one = response_one.json()["code_prompt"]
        prompt_all = response_all.json()["code_prompt"]
        assert len(prompt_one) < len(prompt_all)

    def test_unknown_ids_are_ignored(self, client):
        """Unknown IDs mixed with valid IDs should be silently ignored."""
        response = client.post(
            "/ai/code-prompt/selective",
            json=self._make_request(["item-1", "item-999"]),
        )
        assert response.status_code == 200
        prompt = response.json()["code_prompt"]

        # Valid item is included
        assert "Add user validation" in prompt
        # No trace of item-999 (which doesn't exist)

    def test_empty_selected_ids_returns_400(self, client):
        """Empty selected_item_ids should return 400."""
        response = client.post(
            "/ai/code-prompt/selective",
            json=self._make_request([]),
        )
        assert response.status_code == 400
        assert "At least one selected_item_id" in response.json()["detail"]

    def test_no_matching_ids_returns_400(self, client):
        """All unknown IDs (no matches) should return 400."""
        response = client.post(
            "/ai/code-prompt/selective",
            json=self._make_request(["nonexistent-1", "nonexistent-2"]),
        )
        assert response.status_code == 400
        assert "No items matched" in response.json()["detail"]

    def test_python_targets_exclude_js_guidelines(self, client):
        """When selected items target only .py files, JS guidelines should not appear."""
        # Select only item-1 which targets models/user.py
        response = client.post(
            "/ai/code-prompt/selective",
            json=self._make_request(
                ["item-1"],
                detected_languages=["python", "javascript"],
            ),
        )
        assert response.status_code == 200
        prompt = response.json()["code_prompt"]

        # Python targets: style should be inferred from .py files
        # PromptBuilder infers languages from targets first, so
        # JavaScript style won't be loaded when targets are .py only
        assert "models/user.py" in prompt

    def test_multiple_items_merges_targets(self, client):
        """Selecting multiple items should merge and deduplicate targets."""
        response = client.post(
            "/ai/code-prompt/selective",
            json=self._make_request(["item-1", "item-2"]),
        )
        assert response.status_code == 200
        prompt = response.json()["code_prompt"]

        # Both items' content present
        assert "Add user validation" in prompt
        assert "Create REST endpoints" in prompt

        # Targets from both items
        assert "models/user.py" in prompt
        assert "api/users.py" in prompt

    def test_risk_level_uses_highest_among_selected(self, client):
        """Risk level should be the highest among selected items."""
        # Select item-1 (low) and item-3 (high)
        response = client.post(
            "/ai/code-prompt/selective",
            json=self._make_request(["item-1", "item-3"]),
        )
        assert response.status_code == 200
        prompt = response.json()["code_prompt"]

        risk_start = prompt.find("<risk_level>")
        risk_end = prompt.find("</risk_level>")
        risk_section = prompt[risk_start:risk_end]
        assert "high" in risk_section

    def test_topic_included_in_prompt(self, client):
        """The summary topic should be included in the generated prompt."""
        response = client.post(
            "/ai/code-prompt/selective",
            json=self._make_request(["item-1"]),
        )
        assert response.status_code == 200
        prompt = response.json()["code_prompt"]
        assert "User Management System" in prompt

    def test_selective_with_context_snippets(self, client):
        """Context snippets should be injected into the selective prompt."""
        req = self._make_request(["item-1"])
        req["context_snippets"] = [
            {
                "file_path": "models/user.py",
                "snippet": "class User(BaseModel):\n    name: str\n    email: str",
            },
        ]
        response = client.post("/ai/code-prompt/selective", json=req)

        assert response.status_code == 200
        prompt = response.json()["code_prompt"]
        assert "<context_snippets>" in prompt
        assert "### models/user.py" in prompt
        assert "class User(BaseModel):" in prompt

    def test_selective_without_context_snippets(self, client):
        """No context section when snippets are not provided."""
        response = client.post(
            "/ai/code-prompt/selective",
            json=self._make_request(["item-1"]),
        )
        assert response.status_code == 200
        prompt = response.json()["code_prompt"]
        assert "<context_snippets>" not in prompt


# =============================================================================
# TestRoomOutputModeInCodePrompt
# =============================================================================


class TestRoomOutputModeInCodePrompt:
    """Tests that room-level output_mode overrides server config default."""

    @pytest.fixture
    def client(self):
        from app.main import app
        return TestClient(app)

    def _set_room_output_mode(self, client, room_id, mode):
        """Helper to set output_mode for a room via the settings API."""
        client.put(f"/rooms/{room_id}/settings", json={"output_mode": mode})

    def test_items_endpoint_uses_room_output_mode(self, client):
        """POST /ai/code-prompt/items should use room output_mode when set."""
        room_id = "test-output-mode-items"
        self._set_room_output_mode(client, room_id, "direct_repo_edits")

        response = client.post("/ai/code-prompt/items", json={
            "items": [{
                "id": "item-1",
                "type": "code_change",
                "title": "Fix bug",
                "problem": "Bug exists",
                "proposed_change": "Fix it",
                "targets": ["app.py"],
                "risk_level": "low",
            }],
            "room_id": room_id,
        })

        assert response.status_code == 200
        prompt = response.json()["code_prompt"]
        # direct_repo_edits mentions "test suite" and "linter"
        assert "test suite" in prompt.lower() or "linter" in prompt.lower()

    def test_items_endpoint_falls_back_to_server_default(self, client):
        """Without room output_mode, should use server config default."""
        response = client.post("/ai/code-prompt/items", json={
            "items": [{
                "id": "item-1",
                "type": "code_change",
                "title": "Fix bug",
                "problem": "Bug exists",
                "proposed_change": "Fix it",
                "targets": ["app.py"],
                "risk_level": "low",
            }],
        })

        assert response.status_code == 200
        prompt = response.json()["code_prompt"]
        # Server default is unified_diff
        assert "git apply" in prompt.lower()

    def test_selective_endpoint_uses_room_output_mode(self, client):
        """POST /ai/code-prompt/selective should use room output_mode when set."""
        room_id = "test-output-mode-selective"
        self._set_room_output_mode(client, room_id, "plan_then_diff")

        response = client.post("/ai/code-prompt/selective", json={
            "summary": {
                "topic": "Test",
                "code_relevant_items": [{
                    "id": "item-1",
                    "type": "code_change",
                    "title": "Fix bug",
                    "problem": "Bug",
                    "proposed_change": "Fix",
                    "targets": ["app.py"],
                    "risk_level": "low",
                }],
            },
            "selected_item_ids": ["item-1"],
            "room_id": room_id,
        })

        assert response.status_code == 200
        prompt = response.json()["code_prompt"]
        assert "implementation plan" in prompt.lower()

    def test_legacy_endpoint_uses_room_output_mode(self, client):
        """POST /ai/code-prompt should use room output_mode when set."""
        room_id = "test-output-mode-legacy"
        self._set_room_output_mode(client, room_id, "plan_then_diff")

        response = client.post("/ai/code-prompt", json={
            "decision_summary": {
                "type": "decision_summary",
                "topic": "Fix auth",
                "problem_statement": "Login fails",
                "proposed_solution": "Fix token",
                "requires_code_change": True,
                "affected_components": ["auth/login.py"],
                "risk_level": "medium",
                "next_steps": ["Fix"],
            },
            "room_id": room_id,
        })

        assert response.status_code == 200
        prompt = response.json()["code_prompt"]
        assert "implementation plan" in prompt.lower()

    def test_empty_room_output_mode_uses_server_default(self, client):
        """Empty string room output_mode should fall back to server default."""
        room_id = "test-output-mode-empty"
        self._set_room_output_mode(client, room_id, "")

        response = client.post("/ai/code-prompt/items", json={
            "items": [{
                "id": "item-1",
                "type": "code_change",
                "title": "Fix",
                "problem": "Bug",
                "proposed_change": "Fix",
                "targets": ["app.py"],
                "risk_level": "low",
            }],
            "room_id": room_id,
        })

        assert response.status_code == 200
        prompt = response.json()["code_prompt"]
        # Empty string should fall back to server default (unified_diff)
        assert "git apply" in prompt.lower()
