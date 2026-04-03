"""Interactive agent question/answer coordination.

Provides an in-process registry that lets an agent loop pause on an
``ask_user`` tool call and resume when the user provides an answer via
the REST endpoint ``POST /api/context/query/{session_id}/answer``.

The registry is a module-level dict keyed by ``session_id``.  Both the
SSE response generator and the REST answer endpoint run in the same
uvicorn process, so an ``asyncio.Event`` is sufficient for coordination.
"""

import asyncio
import time
from dataclasses import dataclass, field
from typing import Dict, Optional

# How long the agent loop will wait for a user answer (seconds).
ASK_USER_TIMEOUT: float = 300.0  # 5 minutes


@dataclass
class PendingQuestion:
    """A question the agent is waiting for the user to answer."""

    question: str
    context: str
    event: asyncio.Event = field(default_factory=asyncio.Event)
    answer: Optional[str] = None
    created_at: float = field(default_factory=time.monotonic)
    timed_out: bool = False


# Module-level registry — keyed by session_id
_pending: Dict[str, PendingQuestion] = {}


def register_question(session_id: str, question: str, context: str = "") -> PendingQuestion:
    """Register a pending question and return the ``PendingQuestion`` handle.

    If a question already exists for this session, it is replaced.
    """
    pq = PendingQuestion(question=question, context=context)
    _pending[session_id] = pq
    return pq


def submit_answer(session_id: str, answer: str) -> bool:
    """Submit the user's answer for a pending question.

    Returns ``True`` if the answer was accepted, ``False`` if no pending
    question exists or the question was already answered / timed out.
    """
    pq = _pending.get(session_id)
    if pq is None or pq.event.is_set():
        return False
    pq.answer = answer
    pq.event.set()
    return True


def get_pending(session_id: str) -> Optional[PendingQuestion]:
    """Return the pending question for *session_id*, or ``None``."""
    return _pending.get(session_id)


def cleanup(session_id: str) -> None:
    """Remove the pending question entry for *session_id*."""
    _pending.pop(session_id, None)
