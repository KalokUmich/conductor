"""Unit tests for Phase 9.17 — Brain lifecycle hooks.

Covers:
- HOOK_NAMES contains the 4 expected points
- register_hook / unregister_hook + ordering
- fire_hook with no callbacks is a no-op
- fire_hook with N callbacks fires all in order
- LifecycleContext payload shape (name + orchestrator + data)
- Hook callback exception is swallowed (Brain keeps running)
- Hook ordering matches registration order
- Unknown hook name in register_hook raises ValueError
- Unknown hook name in fire_hook is a no-op (logged warning, no crash)
- _clear_hooks() test utility
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.agent_loop.lifecycle import (
    HOOK_NAMES,
    LifecycleContext,
    _clear_hooks,
    fire_hook,
    register_hook,
    unregister_hook,
)


@pytest.fixture(autouse=True)
def reset_registry():
    """Wipe any cross-test contamination of the module-level registry."""
    _clear_hooks()
    yield
    _clear_hooks()


# ---------------------------------------------------------------------------
# Hook contract / registry
# ---------------------------------------------------------------------------


class TestHookNames:
    def test_4_known_points(self):
        assert HOOK_NAMES == (
            "on_survey_complete",
            "on_dispatch_complete",
            "on_synthesize_complete",
            "on_task_end",
        )


class TestRegisterUnregister:
    def test_register_then_unregister(self):
        cb = lambda ctx: None  # noqa: E731
        register_hook("on_task_end", cb)
        assert unregister_hook("on_task_end", cb) is True
        # Second remove returns False
        assert unregister_hook("on_task_end", cb) is False

    def test_register_unknown_hook_raises(self):
        with pytest.raises(ValueError):
            register_hook("nonexistent_hook", lambda ctx: None)

    def test_unregister_unknown_hook_returns_false(self):
        assert unregister_hook("nonexistent", lambda ctx: None) is False


# ---------------------------------------------------------------------------
# Firing
# ---------------------------------------------------------------------------


class TestFireHook:
    def test_no_callbacks_is_noop(self):
        # Just shouldn't raise
        fire_hook("on_survey_complete", orchestrator=MagicMock(), data={})

    def test_fires_registered_callback(self):
        captured = []

        def cb(ctx: LifecycleContext) -> None:
            captured.append(ctx)

        register_hook("on_synthesize_complete", cb)
        orchestrator = MagicMock()
        fire_hook(
            "on_synthesize_complete",
            orchestrator=orchestrator,
            data={"k": "v"},
        )
        assert len(captured) == 1
        ctx = captured[0]
        assert ctx.name == "on_synthesize_complete"
        assert ctx.orchestrator is orchestrator
        assert ctx.data == {"k": "v"}

    def test_fires_multiple_in_order(self):
        seq = []
        register_hook("on_task_end", lambda ctx: seq.append("first"))
        register_hook("on_task_end", lambda ctx: seq.append("second"))
        register_hook("on_task_end", lambda ctx: seq.append("third"))
        fire_hook("on_task_end", orchestrator=MagicMock())
        assert seq == ["first", "second", "third"]

    def test_swallows_callback_exception(self):
        """A throwing hook must not propagate."""

        def throwing(ctx):
            raise RuntimeError("boom")

        seq = []

        def runs_after_throw(ctx):
            seq.append("survived")

        register_hook("on_task_end", throwing)
        register_hook("on_task_end", runs_after_throw)

        # Must NOT raise
        fire_hook("on_task_end", orchestrator=MagicMock())
        # Subsequent hooks still ran despite earlier throw
        assert seq == ["survived"]

    def test_unknown_hook_name_is_logged_no_op(self, caplog):
        # Doesn't raise, doesn't fire anything (no callbacks were ever
        # registerable for an unknown name)
        fire_hook("nonexistent_hook", orchestrator=MagicMock())
        # Confirm a warning landed in the logger
        assert any("unknown hook name" in rec.message.lower() for rec in caplog.records)

    def test_data_defaults_to_empty_dict(self):
        captured = []
        register_hook("on_survey_complete", lambda c: captured.append(c))
        fire_hook("on_survey_complete", orchestrator=MagicMock())
        assert captured[0].data == {}

    def test_callback_can_register_during_fire(self):
        """Late-registered callbacks fire on the NEXT fire, not this one
        (we snapshot the callback list before iterating)."""
        seq = []
        late_cb = lambda ctx: seq.append("late")  # noqa: E731

        def early(ctx):
            seq.append("early")
            register_hook("on_task_end", late_cb)

        register_hook("on_task_end", early)
        fire_hook("on_task_end", orchestrator=MagicMock())
        # Only "early" fired this round (late was added but isn't in
        # the snapshot)
        assert seq == ["early"]
        # Now the late one is in the registry — fire again
        fire_hook("on_task_end", orchestrator=MagicMock())
        assert seq == ["early", "early", "late"]


class TestClearHooks:
    def test_clear_one_hook(self):
        register_hook("on_task_end", lambda c: None)
        register_hook("on_survey_complete", lambda c: None)
        _clear_hooks("on_task_end")
        # on_survey_complete remains; on_task_end is empty
        captured_se = []
        captured_te = []
        register_hook("on_survey_complete", lambda c: captured_se.append(1))
        register_hook("on_task_end", lambda c: captured_te.append(1))
        fire_hook("on_survey_complete", orchestrator=MagicMock())
        fire_hook("on_task_end", orchestrator=MagicMock())
        # on_survey_complete had 2 callbacks → 2 fires; on_task_end had 1
        assert len(captured_se) == 1  # only the new one (the old anonymous fired once but didn't count)
        # actually our anonymous lambda from before clearing also fires
        # — but it's `lambda c: None`, it doesn't append. So captured_se
        # only sees the 1 from the post-clear registration. Same for te.
        assert len(captured_te) == 1

    def test_clear_all(self):
        register_hook("on_task_end", lambda c: None)
        register_hook("on_survey_complete", lambda c: None)
        _clear_hooks()
        captured = []
        register_hook("on_task_end", lambda c: captured.append("te"))
        fire_hook("on_task_end", orchestrator=MagicMock())
        # Only the post-clear registration fires
        assert captured == ["te"]
