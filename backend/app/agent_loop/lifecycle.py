"""Brain lifecycle hooks (Phase 9.17).

The PR Brain v2 pipeline has 4 natural extension points where
third-party (or our own) code can plug in without forking the core
synthesis path. This module defines the hook registry and the firing
mechanism.

Hook points:

    on_survey_complete    — coordinator finished its initial diff +
                            impact-graph survey; about to start Plan
    on_dispatch_complete  — coordinator finished all sub-agent
                            dispatches; about to start the precision
                            filter / synthesis
    on_synthesize_complete — synthesis text + final findings ready;
                            about to emit terminal events
    on_task_end           — terminal hook; fires once per orchestrator
                            run, even on error (use a try/finally
                            wrapper to guarantee). Use for cleanup
                            (scratchpad delete, telemetry export).

Hooks are **fire-and-forget**: the orchestrator does not await
hook results, hook failures are logged but never propagate. This
keeps hooks safe to add (worst case: a hook throws, the orchestrator
keeps going; best case: hook does its work).

Hooks receive a ``LifecycleContext`` carrying:
- the hook name (for branching code that registers one callback for
  multiple points)
- ``orchestrator``: the live PRBrainOrchestrator instance (gives
  access to scratchpad, providers, pr_context if they want to read)
- ``data``: a dict of point-specific keys (see each hook docstring)

Usage (consumer side):

    from app.agent_loop.lifecycle import register_hook

    def my_telemetry(ctx):
        if ctx.name == "on_synthesize_complete":
            metrics.gauge("findings", len(ctx.data["findings"]))

    register_hook("on_synthesize_complete", my_telemetry)

Usage (orchestrator side, internal):

    from app.agent_loop.lifecycle import fire_hook

    fire_hook("on_survey_complete", orchestrator=self, data={
        "pr_context": pr_context,
        "impact_context": impact_context,
    })

The registry is module-level so hooks declared at import time are
picked up automatically. Tests can clear it via ``_clear_hooks()``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Hook contract
# ---------------------------------------------------------------------------


HOOK_NAMES = (
    "on_survey_complete",
    "on_dispatch_complete",
    "on_synthesize_complete",
    "on_task_end",
)


@dataclass
class LifecycleContext:
    """Payload passed to every lifecycle-hook callback.

    ``data`` keys depend on the hook point — see each fire site for
    the contract. Hooks should not mutate ``orchestrator`` or
    ``pr_context``-shaped values; they're read-only by convention.
    """

    name: str
    orchestrator: Any  # PRBrainOrchestrator (avoid circular import)
    data: Dict[str, Any] = field(default_factory=dict)


HookCallback = Callable[[LifecycleContext], None]


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_HOOKS: Dict[str, List[HookCallback]] = {name: [] for name in HOOK_NAMES}


def register_hook(name: str, callback: HookCallback) -> None:
    """Register ``callback`` to fire on ``name`` lifecycle event.

    Order of registration is order of firing. Same callback can be
    registered multiple times (it'll fire multiple times) — this is
    rarely what you want, callers usually de-dup themselves.

    Raises ValueError on unknown hook name to catch typos at
    registration time rather than silently dropping callbacks.
    """
    if name not in _HOOKS:
        raise ValueError(f"Unknown lifecycle hook '{name}'. Valid names: {HOOK_NAMES}")
    _HOOKS[name].append(callback)


def unregister_hook(name: str, callback: HookCallback) -> bool:
    """Remove a previously-registered callback. Returns True if the
    callback was found and removed, False otherwise. Useful for tests
    that want to assert hook isolation."""
    if name not in _HOOKS:
        return False
    try:
        _HOOKS[name].remove(callback)
    except ValueError:
        return False
    return True


def _clear_hooks(name: Optional[str] = None) -> None:
    """Clear all callbacks for ``name`` (or every hook when None).

    Test-only utility — production code should not need to clear the
    registry. Marked with leading underscore to make that intent
    explicit.
    """
    if name is None:
        for k in _HOOKS:
            _HOOKS[k].clear()
    elif name in _HOOKS:
        _HOOKS[name].clear()


# ---------------------------------------------------------------------------
# Firing
# ---------------------------------------------------------------------------


def fire_hook(
    name: str,
    *,
    orchestrator: Any,
    data: Optional[Dict[str, Any]] = None,
) -> None:
    """Fire all callbacks registered for ``name``. Errors are logged
    and swallowed — hooks must never crash the Brain.

    Synchronous on purpose. Hooks should be cheap (telemetry, cleanup,
    extraction). If a hook needs to do async work, it should kick off
    its own task via ``asyncio.create_task`` and return immediately.
    """
    if name not in _HOOKS:
        logger.warning("fire_hook: unknown hook name '%s' (no-op)", name)
        return

    callbacks = list(_HOOKS[name])  # snapshot to allow re-registration during fire
    if not callbacks:
        return

    ctx = LifecycleContext(
        name=name,
        orchestrator=orchestrator,
        data=data or {},
    )
    for cb in callbacks:
        try:
            cb(ctx)
        except Exception as exc:
            cb_name = getattr(cb, "__qualname__", repr(cb))
            logger.warning(
                "lifecycle hook '%s' callback %s raised %s: %s — " "swallowed so the Brain keeps running",
                name,
                cb_name,
                type(exc).__name__,
                exc,
            )


__all__ = [
    "HOOK_NAMES",
    "HookCallback",
    "LifecycleContext",
    "_clear_hooks",
    "fire_hook",
    "register_hook",
    "unregister_hook",
]
