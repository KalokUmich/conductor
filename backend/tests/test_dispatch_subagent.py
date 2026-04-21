"""Unit tests for PR Brain v2's dispatch_subagent primitive.

Covers:
- Pydantic schema validation (scope 1-5 files, exactly 3 checks)
- Depth wall (depth >= 2 rejected)
- JSON output parser (fenced blocks, prose-embedded, malformed)
- severity=null enforcement on findings the worker returns

Does NOT run a real sub-agent (that requires a Bedrock call); uses a
fake _dispatch_agent that returns canned JSON answers.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.agent_loop.brain import AgentToolExecutor, _parse_subagent_json
from app.code_tools.schemas import DispatchSubagentParams, ToolResult

# ---------------------------------------------------------------------------
# Pydantic schema validation
# ---------------------------------------------------------------------------


class TestDispatchSubagentParams:
    def test_minimum_valid_params(self):
        p = DispatchSubagentParams(
            scope=[{"file": "x.py", "start": 1, "end": 50}],
            checks=["q1", "q2", "q3"],
            success_criteria="answer each check with evidence",
        )
        assert len(p.scope) == 1
        assert len(p.checks) == 3
        assert p.may_subdispatch is False
        assert p.model_tier == "explorer"

    def test_five_file_scope_allowed(self):
        """Revised limit from v2 plan: 5 files (was 3)."""
        p = DispatchSubagentParams(
            scope=[{"file": f"f{i}.py"} for i in range(5)],
            checks=["q1", "q2", "q3"],
            success_criteria="answer each",
        )
        assert len(p.scope) == 5

    def test_six_files_rejected(self):
        with pytest.raises(ValidationError):
            DispatchSubagentParams(
                scope=[{"file": f"f{i}.py"} for i in range(6)],
                checks=["q1", "q2", "q3"],
                success_criteria="answer each",
            )

    def test_exactly_three_checks_required(self):
        """2 or 4 checks must be rejected — Brain's plan must split."""
        with pytest.raises(ValidationError):
            DispatchSubagentParams(
                scope=[{"file": "x.py"}],
                checks=["q1", "q2"],
                success_criteria="answer each",
            )
        with pytest.raises(ValidationError):
            DispatchSubagentParams(
                scope=[{"file": "x.py"}],
                checks=["q1", "q2", "q3", "q4"],
                success_criteria="answer each",
            )

    def test_scope_line_range_optional(self):
        p = DispatchSubagentParams(
            scope=[{"file": "whole_file.py"}],  # no start/end = whole file
            checks=["q1", "q2", "q3"],
            success_criteria="answer each",
        )
        assert p.scope[0].start is None
        assert p.scope[0].end is None


# ---------------------------------------------------------------------------
# JSON parser for sub-agent output
# ---------------------------------------------------------------------------


class TestParseSubagentJson:
    def test_fenced_json_block(self):
        raw = """Here is my analysis:

```json
{"checks": [{"id": "check_1", "verdict": "confirmed"}],
 "findings": [],
 "unexpected_observations": []}
```
"""
        parsed = _parse_subagent_json(raw)
        assert parsed is not None
        assert parsed["checks"][0]["id"] == "check_1"

    def test_last_fenced_block_wins(self):
        """If the model restates its answer near the end, use the last block."""
        raw = """
```json
{"checks": [{"id": "intermediate"}]}
```

Actually let me reconsider...

```json
{"checks": [{"id": "final"}], "findings": [], "unexpected_observations": []}
```
"""
        parsed = _parse_subagent_json(raw)
        assert parsed["checks"][0]["id"] == "final"

    def test_prose_embedded_json(self):
        """No fences — the parser falls back to finding the last {...} with 'checks'."""
        raw = 'I reviewed it. Result: {"checks": [{"id": "check_1"}], "findings": [], "unexpected_observations": []}. Done.'
        parsed = _parse_subagent_json(raw)
        assert parsed is not None
        assert parsed["checks"][0]["id"] == "check_1"

    def test_no_checks_key_returns_none(self):
        raw = '```json\n{"summary": "all good"}\n```'
        assert _parse_subagent_json(raw) is None

    def test_empty_input_returns_none(self):
        assert _parse_subagent_json("") is None
        assert _parse_subagent_json(None) is None  # type: ignore

    def test_malformed_json_returns_none(self):
        raw = '```json\n{"checks": [unclosed\n```'
        assert _parse_subagent_json(raw) is None


# ---------------------------------------------------------------------------
# Depth wall
# ---------------------------------------------------------------------------


class TestDepthWall:
    """Depth 2 is a hard wall — sub-sub-agents cannot call dispatch_subagent.

    We exercise ``AgentToolExecutor._dispatch_subagent`` directly at each
    depth without a real sub-agent run (fake the inner dispatch to isolate
    the depth-check logic)."""

    def _make_executor(self, depth: int):
        from app.agent_loop.config import BrainExecutorConfig

        class _NullInner:
            async def execute(self, *a, **kw):
                return ToolResult(tool_name="noop", success=True, data={})

        return AgentToolExecutor(
            inner_executor=_NullInner(),
            agent_registry={},
            swarm_registry={},
            agent_provider=AsyncMock(),
            config=BrainExecutorConfig(
                workspace_path="/tmp",
                current_depth=depth,
                max_depth=2,
                max_concurrent=3,
                sub_agent_timeout=60,
            ),
        )

    @pytest.mark.asyncio
    async def test_depth_2_rejected(self):
        executor = self._make_executor(depth=2)
        r = await executor._dispatch_subagent({
            "scope": [{"file": "x.py"}],
            "checks": ["q1", "q2", "q3"],
            "success_criteria": "...",
        })
        assert not r.success
        assert "depth" in r.error.lower()

    @pytest.mark.asyncio
    async def test_depth_3_rejected(self):
        """Even if somehow invoked at depth 3 (bug), the wall still holds."""
        executor = self._make_executor(depth=3)
        r = await executor._dispatch_subagent({
            "scope": [{"file": "x.py"}],
            "checks": ["q1", "q2", "q3"],
            "success_criteria": "...",
        })
        assert not r.success

    @pytest.mark.asyncio
    async def test_scope_too_large_rejected(self):
        executor = self._make_executor(depth=0)
        r = await executor._dispatch_subagent({
            "scope": [{"file": f"f{i}.py"} for i in range(6)],  # 6 > 5
            "checks": ["q1", "q2", "q3"],
            "success_criteria": "...",
        })
        assert not r.success
        assert "scope" in r.error.lower()

    @pytest.mark.asyncio
    async def test_wrong_check_count_rejected(self):
        executor = self._make_executor(depth=0)
        r = await executor._dispatch_subagent({
            "scope": [{"file": "x.py"}],
            "checks": ["q1", "q2"],  # 2 != 3
            "success_criteria": "...",
        })
        assert not r.success
        assert "3 checks" in r.error

    @pytest.mark.asyncio
    async def test_missing_template_reports_error(self):
        """If pr_subagent_checks agent isn't registered, a clear error."""
        executor = self._make_executor(depth=0)
        r = await executor._dispatch_subagent({
            "scope": [{"file": "x.py"}],
            "checks": ["q1", "q2", "q3"],
            "success_criteria": "...",
        })
        assert not r.success
        assert "pr_subagent_checks" in r.error


# ---------------------------------------------------------------------------
# Severity=null enforcement on returned findings
# ---------------------------------------------------------------------------


class TestSeverityNullEnforcement:
    """Workers must NOT classify severity. Even if a sub-agent emits a
    severity field, we null it out before returning to Brain."""

    @pytest.mark.asyncio
    async def test_severity_nulled_even_if_worker_sets_it(self, monkeypatch):
        from app.workflow.models import AgentConfig, AgentLimits

        # Fake the agent registry with a valid template
        registry = {
            "pr_subagent_checks": AgentConfig(
                name="pr_subagent_checks",
                description="",
                model="explorer",
                instructions="",
                skill="pr_subagent_checks",
                tools=["grep"],
                limits=AgentLimits(max_iterations=5, budget_tokens=100_000, evidence_retries=1),
            ),
        }

        # Fake _dispatch_agent output: contains severity='critical' in findings.
        fake_answer_with_severity = """```json
{
  "checks": [{"id": "check_1", "verdict": "violated", "evidence": "line 42"}],
  "findings": [
    {"title": "bug", "file": "x.py", "line": 42, "severity": "critical", "confidence": 0.9}
  ],
  "unexpected_observations": []
}
```"""

        class _FakeInner:
            async def execute(self, *a, **kw):
                return ToolResult(tool_name="noop", success=True, data={})

        from app.agent_loop.config import BrainExecutorConfig

        executor = AgentToolExecutor(
            inner_executor=_FakeInner(),
            agent_registry=registry,
            swarm_registry={},
            agent_provider=AsyncMock(),
            config=BrainExecutorConfig(
                workspace_path="/tmp",
                current_depth=0,
                max_depth=2,
                max_concurrent=3,
                sub_agent_timeout=60,
            ),
        )

        async def _fake_dispatch_agent(params):
            return ToolResult(
                tool_name="dispatch_agent",
                success=True,
                data={
                    "answer": fake_answer_with_severity,
                    "iterations": 5,
                    "total_input_tokens": 1000,
                    "total_output_tokens": 500,
                    "files_accessed": ["x.py"],
                },
            )

        monkeypatch.setattr(executor, "_dispatch_agent", _fake_dispatch_agent)

        result = await executor._dispatch_subagent({
            "scope": [{"file": "x.py"}],
            "checks": ["q1", "q2", "q3"],
            "success_criteria": "answer each",
        })
        assert result.success
        assert result.data["findings"][0]["severity"] is None  # NULLED
        assert result.data["findings"][0]["title"] == "bug"


# ---------------------------------------------------------------------------
# Schema — role mode (P12)
# ---------------------------------------------------------------------------


class TestRoleModeSchema:
    """Role dispatch makes checks optional but requires role XOR checks."""

    def test_role_only_valid(self):
        p = DispatchSubagentParams(
            scope=[{"file": "x.py"}],
            role="security",
            direction_hint="look for token leaks",
            success_criteria="Return findings with evidence",
        )
        assert p.role == "security"
        assert p.checks is None

    def test_role_with_checks_valid(self):
        p = DispatchSubagentParams(
            scope=[{"file": "x.py"}],
            role="security",
            checks=["q1", "q2", "q3"],
            success_criteria="Return findings with evidence",
        )
        assert p.role == "security"
        assert len(p.checks) == 3

    def test_neither_rejected(self):
        with pytest.raises(ValidationError):
            DispatchSubagentParams(
                scope=[{"file": "x.py"}],
                success_criteria="Return findings with evidence",
            )

    def test_wrong_check_count_with_role_rejected(self):
        with pytest.raises(ValidationError):
            DispatchSubagentParams(
                scope=[{"file": "x.py"}],
                role="security",
                checks=["q1", "q2"],  # 2 != 3
                success_criteria="Return findings with evidence",
            )


# ---------------------------------------------------------------------------
# Role-factory template loader (P12)
# ---------------------------------------------------------------------------


class TestLoadRoleTemplate:
    """The _load_role_template helper finds + parses agent_factory files."""

    def test_loads_security_template(self):
        from app.agent_loop.brain import _load_role_template

        tpl = _load_role_template("security")
        assert tpl is not None
        fm = tpl["frontmatter"]
        assert fm["name"] == "security"
        assert "description" in fm
        assert "tools_hint" in fm
        body = tpl["body"]
        # 4-section convention
        for section in ("## Lens", "## Typical concerns",
                        "## Investigation approach",
                        "## Finding-shape examples"):
            assert section in body, f"missing {section!r} in security template"

    def test_all_6_factory_roles_parseable(self):
        from app.agent_loop.brain import _VALID_FACTORY_ROLES, _load_role_template

        for role in _VALID_FACTORY_ROLES:
            tpl = _load_role_template(role)
            assert tpl is not None, f"{role}.md failed to load"
            assert tpl["frontmatter"].get("name") == role

    def test_unknown_role_returns_none(self):
        from app.agent_loop.brain import _load_role_template

        assert _load_role_template("fake_role") is None


# ---------------------------------------------------------------------------
# Role-mode prompt composition (P12)
# ---------------------------------------------------------------------------


class TestComposeRoleSystemPrompt:
    def _template_stub(self) -> dict:
        return {
            "frontmatter": {
                "name": "security",
                "description": "Attacker mindset",
                "tools_hint": ["grep"],
            },
            "body": (
                "## Lens\nAttacker view.\n\n"
                "## Typical concerns\n- SQL injection\n- Auth bypass\n\n"
                "## Investigation approach\nTrace input to sink.\n\n"
                "## Finding-shape examples\n<example>...</example>"
            ),
        }

    def test_composes_role_context_and_scope(self):
        from app.agent_loop.brain import _compose_role_system_prompt

        out = _compose_role_system_prompt(
            role="security",
            role_template=self._template_stub(),
            scope_block="- src/auth/oauth.py:100-150",
            direction_hint="look for token leaks",
            checks=None,
            brain_context="PR adds PKCE support",
            may_subdispatch=False,
        )
        # Contains role template content
        assert "Attacker view" in out
        assert "SQL injection" in out
        # Contains PR-specific additions
        assert "src/auth/oauth.py:100-150" in out
        assert "look for token leaks" in out
        assert "PR adds PKCE support" in out
        # Contains output contract
        assert '"severity": null' in out
        assert "severity_hint" in out

    def test_with_checks_includes_checks_section(self):
        from app.agent_loop.brain import _compose_role_system_prompt

        out = _compose_role_system_prompt(
            role="security",
            role_template=self._template_stub(),
            scope_block="- foo.py",
            direction_hint=None,
            checks=["Is X validated?", "Does Y leak?", "Is Z authenticated?"],
            brain_context=None,
            may_subdispatch=False,
        )
        assert "Is X validated?" in out
        assert "Does Y leak?" in out

    def test_may_subdispatch_only_emitted_when_true(self):
        from app.agent_loop.brain import _compose_role_system_prompt

        off = _compose_role_system_prompt(
            role="security", role_template=self._template_stub(),
            scope_block="- x.py", direction_hint="d", checks=None,
            brain_context=None, may_subdispatch=False,
        )
        assert "may_subdispatch=true" not in off
        on = _compose_role_system_prompt(
            role="security", role_template=self._template_stub(),
            scope_block="- x.py", direction_hint="d", checks=None,
            brain_context=None, may_subdispatch=True,
        )
        assert "may_subdispatch=true" in on


# ---------------------------------------------------------------------------
# Integration — dispatch_subagent in role mode with a fake _dispatch_agent
# ---------------------------------------------------------------------------


class TestDispatchSubagentRoleMode:
    def _make_executor(self):
        from app.agent_loop.config import BrainExecutorConfig

        class _NullInner:
            async def execute(self, *a, **kw):
                return ToolResult(tool_name="noop", success=True, data={})

        return AgentToolExecutor(
            inner_executor=_NullInner(),
            agent_registry={},
            swarm_registry={},
            agent_provider=AsyncMock(),
            config=BrainExecutorConfig(
                workspace_path="/tmp",
                current_depth=0,
                max_depth=2,
                max_concurrent=3,
                sub_agent_timeout=60,
            ),
        )

    @pytest.mark.asyncio
    async def test_role_dispatch_uses_dynamic_mode(self, monkeypatch):
        """role= should route through dynamic _dispatch_agent with perspective,
        not through template mode."""
        executor = self._make_executor()
        captured = {}

        async def _fake_dispatch_agent(params):
            captured.update(params)
            return ToolResult(
                tool_name="dispatch_agent",
                success=True,
                data={
                    "answer": json.dumps({
                        "summary": "nothing found",
                        "findings": [],
                    }),
                    "iterations": 2,
                    "total_input_tokens": 500,
                    "total_output_tokens": 100,
                    "files_accessed": [],
                },
            )

        monkeypatch.setattr(executor, "_dispatch_agent", _fake_dispatch_agent)

        result = await executor._dispatch_subagent({
            "scope": [{"file": "src/auth/oauth.py", "start": 100, "end": 150}],
            "role": "security",
            "direction_hint": "new PKCE support — look for token leaks",
            "context": "PR adds OAuth PKCE",
            "success_criteria": "answer with evidence",
        })

        assert result.success
        # Dynamic mode: perspective is set, not template
        assert "perspective" in captured
        assert "template" not in captured
        # Perspective includes the role lens + PR context
        assert "security reviewer" in captured["perspective"]
        assert "src/auth/oauth.py:100-150" in captured["perspective"]
        assert "token leaks" in captured["perspective"]
        assert "PKCE" in captured["perspective"]
        # Tools from the factory template
        assert "grep" in captured["tools"]

    @pytest.mark.asyncio
    async def test_unknown_role_rejected(self):
        executor = self._make_executor()
        r = await executor._dispatch_subagent({
            "scope": [{"file": "x.py"}],
            "role": "not_a_real_role",
            "success_criteria": "...",
        })
        assert not r.success
        assert "Unknown role" in r.error

    @pytest.mark.asyncio
    async def test_neither_role_nor_checks_rejected(self):
        executor = self._make_executor()
        r = await executor._dispatch_subagent({
            "scope": [{"file": "x.py"}],
            "success_criteria": "...",
        })
        assert not r.success
        assert "either" in r.error.lower()


# ---------------------------------------------------------------------------
# P10 — model_tier override (strong) propagates through role-mode dispatch
# ---------------------------------------------------------------------------


class TestModelTierOverride:
    """P10: Coordinator's explicit `model_tier="strong"` must override the
    role template's default `model_hint` (most roles default to explorer).
    Explorer default stays explorer."""

    def _make_executor(self):
        from app.agent_loop.config import BrainExecutorConfig

        class _NullInner:
            async def execute(self, *a, **kw):
                return ToolResult(tool_name="noop", success=True, data={})

        return AgentToolExecutor(
            inner_executor=_NullInner(),
            agent_registry={},
            swarm_registry={},
            agent_provider=AsyncMock(),
            config=BrainExecutorConfig(
                workspace_path="/tmp",
                current_depth=0,
                max_depth=2,
                max_concurrent=3,
                sub_agent_timeout=60,
            ),
        )

    @pytest.mark.asyncio
    async def test_role_dispatch_strong_override(self, monkeypatch):
        """role=security (default model_hint=explorer) + coordinator sets
        model_tier=strong → worker dispatched with model=strong."""
        executor = self._make_executor()
        captured = {}

        async def _fake(params):
            captured.update(params)
            return ToolResult(
                tool_name="dispatch_agent",
                success=True,
                data={
                    "answer": json.dumps({"summary": "", "findings": []}),
                    "iterations": 1, "total_input_tokens": 100,
                    "total_output_tokens": 50, "files_accessed": [],
                },
            )
        monkeypatch.setattr(executor, "_dispatch_agent", _fake)

        r = await executor._dispatch_subagent({
            "scope": [{"file": "src/auth/oauth.py"}],
            "role": "security",
            "direction_hint": "cross-file token lifecycle",
            "success_criteria": "any exposure",
            "model_tier": "strong",
        })
        assert r.success
        assert captured["model"] == "strong"

    @pytest.mark.asyncio
    async def test_role_dispatch_explorer_default(self, monkeypatch):
        """No model_tier override → role template's default (explorer) wins."""
        executor = self._make_executor()
        captured = {}

        async def _fake(params):
            captured.update(params)
            return ToolResult(
                tool_name="dispatch_agent",
                success=True,
                data={
                    "answer": json.dumps({"summary": "", "findings": []}),
                    "iterations": 1, "total_input_tokens": 100,
                    "total_output_tokens": 50, "files_accessed": [],
                },
            )
        monkeypatch.setattr(executor, "_dispatch_agent", _fake)

        r = await executor._dispatch_subagent({
            "scope": [{"file": "src/auth/oauth.py"}],
            "role": "security",
            "direction_hint": "normal review",
            "success_criteria": "any exposure",
        })
        assert r.success
        # security role defaults to explorer in config/agent_factory/security.md
        assert captured["model"] == "explorer"

    @pytest.mark.asyncio
    async def test_correctness_role_uses_strong_by_default(self, monkeypatch):
        """correctness has model_hint=strong in its factory template;
        coordinator doesn't need to override."""
        executor = self._make_executor()
        captured = {}

        async def _fake(params):
            captured.update(params)
            return ToolResult(
                tool_name="dispatch_agent",
                success=True,
                data={
                    "answer": json.dumps({"summary": "", "findings": []}),
                    "iterations": 1, "total_input_tokens": 100,
                    "total_output_tokens": 50, "files_accessed": [],
                },
            )
        monkeypatch.setattr(executor, "_dispatch_agent", _fake)

        r = await executor._dispatch_subagent({
            "scope": [{"file": "x.py"}],
            "role": "correctness",
            "direction_hint": "check invariants",
            "success_criteria": "any defect",
        })
        assert r.success
        assert captured["model"] == "strong"


# ---------------------------------------------------------------------------
# P4 — Plan memory: dispatches auto-logged, recap surfaces on #3+
# ---------------------------------------------------------------------------


class TestPlanMemory:
    """P4: each depth-0 dispatch writes an entry; from dispatch #3 onward
    the tool result includes a `_plan_recap` string listing prior dispatches
    so the coordinator's in-context plan doesn't drift as the loop grows."""

    def _make_executor_with_registry(self):
        from app.agent_loop.config import BrainExecutorConfig
        from app.workflow.models import AgentConfig, AgentLimits

        registry = {
            "pr_subagent_checks": AgentConfig(
                name="pr_subagent_checks",
                description="",
                model="explorer",
                instructions="",
                skill="pr_subagent_checks",
                tools=["grep"],
                limits=AgentLimits(max_iterations=5, budget_tokens=100_000, evidence_retries=1),
            ),
        }

        class _FakeInner:
            async def execute(self, *a, **kw):
                return ToolResult(tool_name="noop", success=True, data={})

        return AgentToolExecutor(
            inner_executor=_FakeInner(),
            agent_registry=registry,
            swarm_registry={},
            agent_provider=AsyncMock(),
            config=BrainExecutorConfig(
                workspace_path="/tmp",
                current_depth=0,
                max_depth=2,
                max_concurrent=3,
                sub_agent_timeout=60,
            ),
        )

    @pytest.fixture
    def store_bound(self, tmp_path, monkeypatch):
        import uuid as _uuid

        from app.scratchpad import FactStore
        from app.scratchpad.context import _current_store

        monkeypatch.setattr("app.scratchpad.store.SCRATCHPAD_ROOT", tmp_path)
        s = FactStore.open(f"t-{_uuid.uuid4().hex[:8]}", workspace="/tmp")
        tok = _current_store.set(s)
        try:
            yield s
        finally:
            _current_store.reset(tok)
            s.delete()

    @pytest.mark.asyncio
    async def test_dispatch_writes_plan_entry(self, monkeypatch, store_bound):
        executor = self._make_executor_with_registry()

        async def _fake(params):
            return ToolResult(
                tool_name="dispatch_agent",
                success=True,
                data={
                    "answer": json.dumps({"checks": [], "findings": [],
                                           "unexpected_observations": []}),
                    "iterations": 1, "total_input_tokens": 100,
                    "total_output_tokens": 50, "files_accessed": [],
                },
            )
        monkeypatch.setattr(executor, "_dispatch_agent", _fake)

        r = await executor._dispatch_subagent({
            "scope": [{"file": "a.py", "start": 10, "end": 30}],
            "checks": ["q1", "q2", "q3"],
            "success_criteria": "answer each check with evidence",
        })
        assert r.success
        entries = store_bound.iter_plan_entries()
        assert len(entries) == 1
        assert entries[0].dispatch_index == 1
        assert entries[0].mode == "checks"
        assert entries[0].role is None
        assert "a.py:10-30" in entries[0].scope

    @pytest.mark.asyncio
    async def test_recap_absent_on_first_two_dispatches(self, monkeypatch, store_bound):
        executor = self._make_executor_with_registry()

        async def _fake(params):
            return ToolResult(
                tool_name="dispatch_agent",
                success=True,
                data={
                    "answer": json.dumps({"checks": [], "findings": [],
                                           "unexpected_observations": []}),
                    "iterations": 1, "total_input_tokens": 100,
                    "total_output_tokens": 50, "files_accessed": [],
                },
            )
        monkeypatch.setattr(executor, "_dispatch_agent", _fake)

        for i in range(2):
            r = await executor._dispatch_subagent({
                "scope": [{"file": f"f{i}.py"}],
                "checks": ["q1", "q2", "q3"],
                "success_criteria": "answer each check with evidence",
            })
            assert r.success
            assert "_plan_recap" not in r.data

    @pytest.mark.asyncio
    async def test_recap_surfaces_on_third_dispatch(self, monkeypatch, store_bound):
        executor = self._make_executor_with_registry()

        async def _fake(params):
            return ToolResult(
                tool_name="dispatch_agent",
                success=True,
                data={
                    "answer": json.dumps({"checks": [], "findings": [],
                                           "unexpected_observations": []}),
                    "iterations": 1, "total_input_tokens": 100,
                    "total_output_tokens": 50, "files_accessed": [],
                },
            )
        monkeypatch.setattr(executor, "_dispatch_agent", _fake)

        for i in range(3):
            r = await executor._dispatch_subagent({
                "scope": [{"file": f"f{i}.py"}],
                "checks": ["q1", "q2", "q3"],
                "success_criteria": f"criteria-{i}-payload-meaningful",
            })
            assert r.success

        assert "_plan_recap" in r.data
        recap = r.data["_plan_recap"]
        assert "3 dispatches so far" in recap
        assert "#1" in recap and "#2" in recap and "#3" in recap
        assert "f0.py" in recap and "f1.py" in recap and "f2.py" in recap

    @pytest.mark.asyncio
    async def test_role_dispatch_records_reason_and_mode(self, monkeypatch, store_bound):
        executor = self._make_executor_with_registry()

        async def _fake(params):
            return ToolResult(
                tool_name="dispatch_agent",
                success=True,
                data={
                    "answer": json.dumps({"summary": "ok", "findings": []}),
                    "iterations": 1, "total_input_tokens": 100,
                    "total_output_tokens": 50, "files_accessed": [],
                },
            )
        monkeypatch.setattr(executor, "_dispatch_agent", _fake)

        r = await executor._dispatch_subagent({
            "scope": [{"file": "src/auth/oauth.py"}],
            "role": "security",
            "direction_hint": "token leaks in refresh flow",
            "success_criteria": "any token exposure or bypass",
        })
        assert r.success
        entries = store_bound.iter_plan_entries()
        assert len(entries) == 1
        assert entries[0].mode == "role"
        assert entries[0].role == "security"
        assert entries[0].reason == "token leaks in refresh flow"

    @pytest.mark.asyncio
    async def test_no_recording_when_no_scratchpad(self, monkeypatch):
        """Depth-0 dispatches with no bound FactStore should succeed silently —
        plan_memory is an enhancement, not a gate."""
        from app.scratchpad.context import _current_store
        tok = _current_store.set(None)
        try:
            executor = self._make_executor_with_registry()

            async def _fake(params):
                return ToolResult(
                    tool_name="dispatch_agent",
                    success=True,
                    data={
                        "answer": json.dumps({"checks": [], "findings": [],
                                               "unexpected_observations": []}),
                        "iterations": 1, "total_input_tokens": 100,
                        "total_output_tokens": 50, "files_accessed": [],
                    },
                )
            monkeypatch.setattr(executor, "_dispatch_agent", _fake)

            r = await executor._dispatch_subagent({
                "scope": [{"file": "x.py"}],
                "checks": ["q1", "q2", "q3"],
                "success_criteria": "answer each check",
            })
            assert r.success
            assert "_plan_recap" not in r.data
        finally:
            _current_store.reset(tok)
