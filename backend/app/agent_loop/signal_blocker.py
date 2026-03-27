"""Signal blocker — sub-agent mid-execution communication with Brain.

When a sub-agent encounters ambiguity or needs direction it can't
determine from the codebase, it calls the ``signal_blocker`` tool.
This pauses the sub-agent's loop and sends the signal to Brain,
which decides: answer from cache, ask the user, or redirect.

The mechanism mirrors ``interactive.py`` (ask_user) but the response
comes from the Brain orchestrator rather than the user.
"""

import asyncio
import time
from dataclasses import dataclass, field
from typing import Dict, Optional


# How long the sub-agent waits for Brain's response (seconds).
SIGNAL_TIMEOUT: float = 60.0  # 1 minute — Brain should respond quickly


@dataclass
class PendingSignal:
    """A signal from a sub-agent waiting for Brain's response."""

    reason: str
    options: list = field(default_factory=list)
    context: str = ""
    event: asyncio.Event = field(default_factory=asyncio.Event)
    response: Optional[str] = None
    created_at: float = field(default_factory=time.monotonic)
    timed_out: bool = False


# Module-level registry — keyed by sub-agent session_id
_pending_signals: Dict[str, PendingSignal] = {}


def register_signal(
    session_id: str, reason: str, options: list = None, context: str = "",
) -> PendingSignal:
    """Register a pending signal and return the handle."""
    ps = PendingSignal(reason=reason, options=options or [], context=context)
    _pending_signals[session_id] = ps
    return ps


def respond_to_signal(session_id: str, response: str) -> bool:
    """Brain responds to a sub-agent's signal.

    Returns True if the response was accepted.
    """
    ps = _pending_signals.get(session_id)
    if ps is None or ps.event.is_set():
        return False
    ps.response = response
    ps.event.set()
    return True


def get_pending_signal(session_id: str) -> Optional[PendingSignal]:
    """Return the pending signal for *session_id*, or None."""
    return _pending_signals.get(session_id)


def cleanup_signal(session_id: str) -> None:
    """Remove the pending signal entry."""
    _pending_signals.pop(session_id, None)
