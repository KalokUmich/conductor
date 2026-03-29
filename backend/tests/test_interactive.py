"""Tests for the interactive agent question/answer registry."""

import asyncio

import pytest

from app.agent_loop.interactive import (
    PendingQuestion,
    cleanup,
    get_pending,
    register_question,
    submit_answer,
    _pending,
)


@pytest.fixture(autouse=True)
def _clear_registry():
    """Ensure the module-level registry is empty before and after each test."""
    _pending.clear()
    yield
    _pending.clear()


def test_register_and_get():
    pq = register_question("sess1", "What API version?", "v1 or v2")
    assert isinstance(pq, PendingQuestion)
    assert pq.question == "What API version?"
    assert pq.context == "v1 or v2"
    assert not pq.event.is_set()
    assert pq.answer is None
    assert get_pending("sess1") is pq


def test_submit_answer():
    pq = register_question("sess2", "Which auth?", "")
    assert submit_answer("sess2", "OAuth 2.0")
    assert pq.answer == "OAuth 2.0"
    assert pq.event.is_set()


def test_submit_answer_nonexistent():
    assert not submit_answer("nonexistent", "answer")


def test_submit_answer_already_answered():
    register_question("sess3", "Q?", "")
    submit_answer("sess3", "A1")
    # Second submit should fail
    assert not submit_answer("sess3", "A2")


def test_cleanup():
    register_question("sess4", "Q?", "")
    assert get_pending("sess4") is not None
    cleanup("sess4")
    assert get_pending("sess4") is None


def test_cleanup_nonexistent():
    # Should not raise
    cleanup("nonexistent")


def test_register_replaces_existing():
    pq1 = register_question("sess5", "Q1?", "")
    pq2 = register_question("sess5", "Q2?", "")
    assert pq1 is not pq2
    assert get_pending("sess5") is pq2
    assert get_pending("sess5").question == "Q2?"


@pytest.mark.asyncio
async def test_event_wait_and_set():
    """Verify that asyncio.Event coordination works end-to-end."""
    pq = register_question("sess6", "What scope?", "")

    async def submit_after_delay():
        await asyncio.sleep(0.05)
        submit_answer("sess6", "module-level")

    asyncio.create_task(submit_after_delay())
    await asyncio.wait_for(pq.event.wait(), timeout=2.0)

    assert pq.answer == "module-level"
    assert pq.event.is_set()
    cleanup("sess6")


@pytest.mark.asyncio
async def test_timeout_scenario():
    """Simulate the timeout path: event never set within the timeout."""
    pq = register_question("sess7", "Q?", "")

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(pq.event.wait(), timeout=0.05)

    # After timeout, mark timed_out manually (as the agent loop would)
    pq.timed_out = True
    assert pq.timed_out
    assert pq.answer is None
    cleanup("sess7")
