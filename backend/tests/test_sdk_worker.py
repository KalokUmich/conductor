"""Tests for SdkWorkerRunner (Step 06b) — no Bedrock.

The SDK ``query()`` is monkeypatched to yield a scripted message stream, so we
test the runner's mapping/evidence-gate/budget logic deterministically.
"""

from __future__ import annotations

import asyncio

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, ToolUseBlock

from app.agent_loop import sdk_worker
from app.agent_loop.sdk_worker import SdkWorkerRunner, _merge_budget, _usage_to_budget_summary
from app.code_tools.schemas import ToolResult


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class _FakeExecutor:
    def __init__(self):
        self.calls = []

    async def execute(self, tool_name, params):
        self.calls.append((tool_name, params))
        return ToolResult(tool_name=tool_name, success=True, data={"ok": True}, error=None)


def _assistant(*blocks):
    return AssistantMessage(content=list(blocks), model="claude-haiku-4-5")


def _result(usage=None, is_error=False, result_text=None):
    return ResultMessage(
        subtype="success",
        duration_ms=100,
        duration_api_ms=80,
        is_error=is_error,
        num_turns=1,
        session_id="sess-1",
        usage=usage,
        result=result_text,
    )


def _tooluse(name, **inp):
    return ToolUseBlock(id="tu1", name=f"mcp__conductor__{name}", input=inp)


def _patch_query(monkeypatch, *streams):
    """Patch sdk_worker.query to yield the given message streams in order
    (one stream per call). Also stub bedrock_env so no real config/creds needed."""
    calls = {"n": 0}

    def fake_query(*, prompt, options):
        idx = min(calls["n"], len(streams) - 1)
        calls["n"] += 1
        msgs = streams[idx]

        async def _gen():
            for m in msgs:
                yield m

        return _gen()

    monkeypatch.setattr(sdk_worker, "query", fake_query)
    monkeypatch.setattr(sdk_worker, "bedrock_env", lambda: {"CLAUDE_CODE_USE_BEDROCK": "1"})
    return calls


def _runner(executor, **kw):
    return SdkWorkerRunner(
        model="eu.anthropic.claude-haiku-4-5-20251001-v1:0",
        tool_executor=executor,
        tool_names=["read_file", "grep", "find_symbol"],
        **kw,
    )


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------
def test_usage_to_budget_summary_keys():
    bs = _usage_to_budget_summary(
        {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 100, "cache_creation_input_tokens": 7}, 3
    )
    assert bs["total_input_tokens"] == 10
    assert bs["total_output_tokens"] == 5
    assert bs["total_tokens"] == 15
    assert bs["cache_read_input_tokens"] == 100
    assert bs["iterations"] == 3


def test_usage_to_budget_summary_handles_none():
    bs = _usage_to_budget_summary(None, 0)
    assert bs["total_input_tokens"] == 0 and bs["total_output_tokens"] == 0


def test_merge_budget_sums():
    m = _merge_budget(
        {"total_input_tokens": 10, "total_output_tokens": 2},
        {"total_input_tokens": 5, "total_output_tokens": 3},
    )
    assert m["total_input_tokens"] == 15 and m["total_output_tokens"] == 5


# ---------------------------------------------------------------------------
# run_once mapping
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_maps_stream_to_agentresult(monkeypatch):
    ex = _FakeExecutor()
    _patch_query(
        monkeypatch,
        [
            _assistant(_tooluse("read_file", path="app/service.py"), TextBlock(text="looking")),
            _assistant(TextBlock(text="Answer: OrderService is in app/service.py:4 and does X.")),
            _result(usage={"input_tokens": 200, "output_tokens": 40}),
        ],
    )
    runner = _runner(ex, max_evidence_retries=0)
    res = await runner.run(system_prompt="SP", user_message="what is OrderService?")

    assert res.error is None
    assert res.tool_calls_made == 1
    assert res.iterations == 2
    assert res.files_accessed == ["app/service.py"]
    assert "OrderService" in res.answer
    assert res.budget_summary["total_input_tokens"] == 200
    # thinking_steps captured the tool call
    assert any(s["kind"] == "tool_result" and s["tool"] == "read_file" for s in res.thinking_steps)


@pytest.mark.asyncio
async def test_error_result_sets_error(monkeypatch):
    ex = _FakeExecutor()
    _patch_query(monkeypatch, [_result(is_error=True, result_text="boom")])
    res = await _runner(ex, max_evidence_retries=0).run(system_prompt="SP", user_message="q")
    assert res.error == "boom"


@pytest.mark.asyncio
async def test_query_exception_becomes_error(monkeypatch):
    ex = _FakeExecutor()

    def boom(*, prompt, options):
        raise RuntimeError("transport down")

    monkeypatch.setattr(sdk_worker, "query", boom)
    monkeypatch.setattr(sdk_worker, "bedrock_env", lambda: {})
    res = await _runner(ex, max_evidence_retries=0).run(system_prompt="SP", user_message="q")
    assert res.error is not None and "transport down" in res.error


# ---------------------------------------------------------------------------
# Evidence gate (post-call re-invoke)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_evidence_retry_fires_on_thin_answer(monkeypatch):
    """First pass: long answer, NO file:line ref, 0 tools → gate fails → re-invoke.
    Second pass: cites a file:line → passes."""
    ex = _FakeExecutor()
    thin = "This is a sufficiently long answer with no citations at all " * 3
    calls = _patch_query(
        monkeypatch,
        [_assistant(TextBlock(text=thin)), _result(usage={"input_tokens": 100, "output_tokens": 10})],
        [
            _assistant(_tooluse("read_file", path="a.py"), _tooluse("grep", pattern="x")),
            _assistant(TextBlock(text="Now with evidence: see app/service.py:42 for the logic.")),
            _result(usage={"input_tokens": 150, "output_tokens": 20}),
        ],
    )
    res = await _runner(ex, max_evidence_retries=1).run(system_prompt="SP", user_message="explain X")
    assert calls["n"] == 2, "should have re-invoked once"
    assert "app/service.py:42" in res.answer
    # budget merged across both passes
    assert res.budget_summary["total_input_tokens"] == 250


@pytest.mark.asyncio
async def test_no_retry_when_answer_has_evidence(monkeypatch):
    ex = _FakeExecutor()
    good = "OrderService lives at app/service.py:4 and calls find_user(). " * 2
    calls = _patch_query(
        monkeypatch,
        [
            _assistant(_tooluse("read_file", path="a.py"), _tooluse("grep", pattern="y")),
            _assistant(TextBlock(text=good)),
            _result(usage={"input_tokens": 100, "output_tokens": 30}),
        ],
    )
    res = await _runner(ex, max_evidence_retries=1).run(system_prompt="SP", user_message="q")
    assert calls["n"] == 1, "good answer should NOT re-invoke"
    assert res.tool_calls_made == 2


@pytest.mark.asyncio
async def test_no_retry_when_budget_zero(monkeypatch):
    ex = _FakeExecutor()
    thin = "Long answer no citation " * 6
    calls = _patch_query(monkeypatch, [_assistant(TextBlock(text=thin)), _result()])
    res = await _runner(ex, max_evidence_retries=0).run(system_prompt="SP", user_message="q")
    assert calls["n"] == 1  # max_evidence_retries=0 → never re-invoke
    assert res.answer.startswith("Long answer")


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_respects_semaphore(monkeypatch):
    ex = _FakeExecutor()
    _patch_query(monkeypatch, [_assistant(TextBlock(text="hi")), _result()])
    sem = asyncio.Semaphore(1)
    runner = _runner(ex, max_evidence_retries=0, llm_semaphore=sem)
    res = await runner.run(system_prompt="SP", user_message="q")
    assert res.error is None
    assert sem._value == 1  # released after use
