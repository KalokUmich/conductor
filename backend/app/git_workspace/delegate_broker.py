"""Delegate Broker — Mode B credential delegation over WebSocket.

When the backend needs credentials for a git operation in Mode B, it:
  1. Sends a *DelegateAuthRequest* to the connected client extension.
  2. Waits (with timeout) for a *DelegateAuthResponse* from the client.
  3. Uses the one-time token for the git operation, then discards it.

Multiple rooms can be handled concurrently; each room has at most one active
WebSocket connection (the Host extension).  Participants that only read code
do not need to connect to this endpoint.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Dict, Optional, Tuple

from fastapi import WebSocket, WebSocketDisconnect

from .schemas import DelegateAuthRequest, DelegateAuthResponse

logger = logging.getLogger(__name__)

# How long to wait for the client to respond to an auth challenge.
_AUTH_TIMEOUT_SECONDS = 30


class DelegateBroker:
    """
    Manages WebSocket connections for Mode B (client credential delegation).

    Lifecycle:
      * One DelegateBroker instance lives on `app.state.delegate_broker`.
      * Each room registers its Host WebSocket via `handle_client`.
      * Service code calls `request_credentials` to obtain a one-time token.
    """

    def __init__(self) -> None:
        # room_id → WebSocket
        self._connections: Dict[str, WebSocket] = {}
        # request_id → asyncio.Future holding the DelegateAuthResponse
        self._pending: Dict[str, asyncio.Future] = {}  # type: ignore[type-arg]

    # ------------------------------------------------------------------
    # WebSocket lifecycle
    # ------------------------------------------------------------------

    async def handle_client(self, room_id: str, ws: WebSocket) -> None:
        """Drive the WebSocket connection for a room's Host client."""
        if room_id in self._connections:
            logger.warning(
                "Replacing existing delegate connection for room %s", room_id
            )
        self._connections[room_id] = ws
        logger.info("Delegate broker: room %s connected", room_id)

        try:
            while True:
                data = await ws.receive_json()
                await self._handle_message(room_id, data)
        except WebSocketDisconnect:
            logger.info("Delegate broker: room %s disconnected", room_id)
        except Exception as exc:  # pylint: disable=broad-except
            logger.error("Delegate broker error for room %s: %s", room_id, exc)
        finally:
            self._connections.pop(room_id, None)
            # Fail all pending requests for this room
            for req_id, fut in list(self._pending.items()):
                if not fut.done():
                    fut.set_exception(
                        ConnectionError(f"Client disconnected for room {room_id}")
                    )

    async def _handle_message(self, room_id: str, data: dict) -> None:
        """Route an incoming message from the client."""
        # We only expect DelegateAuthResponse messages
        try:
            resp = DelegateAuthResponse(**data)
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning(
                "Received unexpected message from room %s: %s (%s)",
                room_id, data, exc,
            )
            return

        fut = self._pending.pop(resp.request_id, None)
        if fut is None:
            logger.warning(
                "No pending request %s for room %s", resp.request_id, room_id
            )
            return
        if not fut.done():
            fut.set_result(resp)

    # ------------------------------------------------------------------
    # Service API
    # ------------------------------------------------------------------

    async def request_credentials(
        self,
        room_id:   str,
        repo_url:  str,
        operation: str,
    ) -> Tuple[str, Optional[str]]:
        """
        Request a one-time credential from the client.

        Returns ``(token, username)`` on success.
        Raises ``ConnectionError`` if no client is connected.
        Raises ``TimeoutError`` if the client does not respond in time.
        """
        ws = self._connections.get(room_id)
        if ws is None:
            raise ConnectionError(
                f"No delegate client connected for room {room_id!r}"
            )

        request_id = str(uuid.uuid4())
        req = DelegateAuthRequest(
            request_id=request_id,
            repo_url=repo_url,
            operation=operation,
        )

        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()  # type: ignore[type-arg]
        self._pending[request_id] = fut

        try:
            await ws.send_json(req.model_dump())
            response: DelegateAuthResponse = await asyncio.wait_for(
                fut, timeout=_AUTH_TIMEOUT_SECONDS
            )
            return response.token, response.username
        except asyncio.TimeoutError as exc:
            self._pending.pop(request_id, None)
            raise TimeoutError(
                f"Client did not respond to auth request {request_id} within "
                f"{_AUTH_TIMEOUT_SECONDS}s"
            ) from exc
        except Exception:
            self._pending.pop(request_id, None)
            raise

    def is_connected(self, room_id: str) -> bool:
        """Return True if a delegate client is currently connected."""
        return room_id in self._connections
