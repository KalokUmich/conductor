"""Redis-backed chat state persistence.

Provides durable storage for chat messages, user state, and room metadata
so that data survives backend restarts.

Redis key schema (prefix ``conductor:``):
  * ``chat:messages:{room_id}`` → List (RPUSH, LRANGE)
  * ``chat:users:{room_id}`` → Hash (user_id → JSON)
  * ``chat:host:{room_id}`` → String (user_id)
  * ``chat:lead:{room_id}`` → String (user_id)
  * ``chat:sso_host:{room_id}`` → Hash (email, provider)
  * ``chat:dedup:{room_id}`` → Set (message IDs)
  * ``chat:read:{room_id}:{msg_id}`` → Set (user IDs)
  * ``chat:settings:{room_id}`` → Hash

TTL: 24 hours, renewed on each write.
"""

from __future__ import annotations

import json
import logging
from typing import Dict, List, Optional, Set

logger = logging.getLogger(__name__)

TTL_SECONDS = 21600  # 6 hours


class RedisChatStore:
    """Redis-backed persistence layer for chat room state."""

    def __init__(self, redis_client, prefix: str = "conductor:") -> None:
        self._redis = redis_client
        self._prefix = prefix

    def _key(self, *parts: str) -> str:
        return f"{self._prefix}{'chat:'}{'_'.join(parts)}"

    def _msg_key(self, room_id: str) -> str:
        return f"{self._prefix}chat:messages:{room_id}"

    def _users_key(self, room_id: str) -> str:
        return f"{self._prefix}chat:users:{room_id}"

    def _host_key(self, room_id: str) -> str:
        return f"{self._prefix}chat:host:{room_id}"

    def _lead_key(self, room_id: str) -> str:
        return f"{self._prefix}chat:lead:{room_id}"

    def _sso_host_key(self, room_id: str) -> str:
        return f"{self._prefix}chat:sso_host:{room_id}"

    def _dedup_key(self, room_id: str) -> str:
        return f"{self._prefix}chat:dedup:{room_id}"

    def _read_key(self, room_id: str, msg_id: str) -> str:
        return f"{self._prefix}chat:read:{room_id}:{msg_id}"

    def _settings_key(self, room_id: str) -> str:
        return f"{self._prefix}chat:settings:{room_id}"

    async def _touch(self, *keys: str) -> None:
        """Renew TTL on keys."""
        pipe = self._redis.pipeline()
        for k in keys:
            pipe.expire(k, TTL_SECONDS)
        await pipe.execute()

    # ------------------------------------------------------------------
    # Messages
    # ------------------------------------------------------------------

    async def append_message(self, room_id: str, message_dict: dict) -> None:
        """Append a message to the room's history."""
        key = self._msg_key(room_id)
        await self._redis.rpush(key, json.dumps(message_dict, default=str))
        await self._touch(key)

    async def get_messages(self, room_id: str) -> List[dict]:
        """Return all messages for a room."""
        key = self._msg_key(room_id)
        raw = await self._redis.lrange(key, 0, -1)
        return [json.loads(m) for m in raw]

    # ------------------------------------------------------------------
    # Users
    # ------------------------------------------------------------------

    async def set_user(self, room_id: str, user_id: str, user_dict: dict) -> None:
        key = self._users_key(room_id)
        await self._redis.hset(key, user_id, json.dumps(user_dict))
        await self._touch(key)

    async def get_users(self, room_id: str) -> Dict[str, dict]:
        key = self._users_key(room_id)
        raw = await self._redis.hgetall(key)
        return {uid: json.loads(data) for uid, data in raw.items()}

    async def remove_user(self, room_id: str, user_id: str) -> None:
        await self._redis.hdel(self._users_key(room_id), user_id)

    # ------------------------------------------------------------------
    # Host / Lead
    # ------------------------------------------------------------------

    async def set_host(self, room_id: str, user_id: str) -> None:
        key = self._host_key(room_id)
        await self._redis.set(key, user_id, ex=TTL_SECONDS)

    async def get_host(self, room_id: str) -> Optional[str]:
        return await self._redis.get(self._host_key(room_id))

    async def set_lead(self, room_id: str, user_id: str) -> None:
        key = self._lead_key(room_id)
        await self._redis.set(key, user_id, ex=TTL_SECONDS)

    async def get_lead(self, room_id: str) -> Optional[str]:
        return await self._redis.get(self._lead_key(room_id))

    # ------------------------------------------------------------------
    # SSO Host Identity
    # ------------------------------------------------------------------

    async def set_sso_host(self, room_id: str, email: str, provider: str) -> None:
        key = self._sso_host_key(room_id)
        await self._redis.hset(key, mapping={"email": email, "provider": provider})
        await self._touch(key)

    async def get_sso_host(self, room_id: str) -> Optional[dict]:
        key = self._sso_host_key(room_id)
        data = await self._redis.hgetall(key)
        return data if data else None

    # ------------------------------------------------------------------
    # Deduplication
    # ------------------------------------------------------------------

    async def is_duplicate(self, room_id: str, message_id: str) -> bool:
        key = self._dedup_key(room_id)
        added = await self._redis.sadd(key, message_id)
        await self._touch(key)
        return added == 0  # 0 means already existed

    # ------------------------------------------------------------------
    # Read receipts
    # ------------------------------------------------------------------

    async def mark_read(self, room_id: str, msg_id: str, user_id: str) -> Set[str]:
        key = self._read_key(room_id, msg_id)
        await self._redis.sadd(key, user_id)
        await self._touch(key)
        members = await self._redis.smembers(key)
        return set(members)

    async def get_read_by(self, room_id: str, msg_id: str) -> Set[str]:
        key = self._read_key(room_id, msg_id)
        members = await self._redis.smembers(key)
        return set(members)

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------

    async def set_settings(self, room_id: str, settings: dict) -> None:
        key = self._settings_key(room_id)
        await self._redis.hset(key, mapping={k: json.dumps(v) for k, v in settings.items()})
        await self._touch(key)

    async def get_settings(self, room_id: str) -> dict:
        key = self._settings_key(room_id)
        raw = await self._redis.hgetall(key)
        result = {}
        for k, v in raw.items():
            try:
                result[k] = json.loads(v)
            except (json.JSONDecodeError, TypeError):
                result[k] = v
        return result

    # ------------------------------------------------------------------
    # Room cleanup
    # ------------------------------------------------------------------

    async def clear_messages(self, room_id: str) -> None:
        """Clear message history for a room."""
        await self._redis.delete(
            self._msg_key(room_id),
            self._dedup_key(room_id),
        )

    async def clear_room(self, room_id: str) -> None:
        """Delete all Redis state for a room."""
        keys = [
            self._msg_key(room_id),
            self._users_key(room_id),
            self._host_key(room_id),
            self._lead_key(room_id),
            self._sso_host_key(room_id),
            self._dedup_key(room_id),
            self._settings_key(room_id),
        ]
        await self._redis.delete(*keys)
