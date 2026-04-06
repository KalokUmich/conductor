"""User profile service — persistent identity across sessions.

Provides get_or_create_user() for SSO login flows. The returned UUID
is stable across all sessions for the same email, enabling consistent
message attribution and participant tracking.
"""

from __future__ import annotations

import logging
import random
from datetime import UTC, datetime
from typing import Optional
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

logger = logging.getLogger(__name__)

# Avatar colors — consistent per user
AVATAR_COLOR_COUNT = 10


class UserService:
    """Singleton service for user profile CRUD."""

    _instance: Optional[UserService] = None

    def __init__(self, engine: AsyncEngine):
        self._session_factory = async_sessionmaker(engine, expire_on_commit=False)

    @classmethod
    def init(cls, engine: AsyncEngine) -> UserService:
        cls._instance = cls(engine)
        return cls._instance

    @classmethod
    def get_instance(cls) -> UserService:
        if not cls._instance:
            raise RuntimeError("UserService not initialized. Call init() first.")
        return cls._instance

    async def get_or_create_user(
        self,
        email: str,
        display_name: Optional[str] = None,
        auth_provider: str = "unknown",
    ) -> dict:
        """Find existing user by email or create a new one.

        Returns a dict with: id, email, display_name, auth_provider, avatar_color
        """
        from app.db.models import User

        normalized_email = email.lower().strip()
        if not normalized_email:
            raise ValueError("Email is required")

        async with self._session_factory() as session:
            # Try to find existing user
            result = await session.execute(select(User).where(User.email == normalized_email))
            user = result.scalar_one_or_none()

            if user:
                # Update last_seen
                await session.execute(update(User).where(User.id == user.id).values(last_seen_at=datetime.now(UTC)))
                await session.commit()
                logger.info(f"[UserService] Existing user: {user.email} (id={user.id})")
                return self._to_dict(user)

            # Create new user
            user_id = str(uuid4())
            name = display_name or normalized_email.split("@")[0]
            avatar_color = random.randint(0, AVATAR_COLOR_COUNT - 1)

            new_user = User(
                id=user_id,
                email=normalized_email,
                display_name=name,
                auth_provider=auth_provider,
                avatar_color=avatar_color,
            )
            session.add(new_user)
            await session.commit()
            logger.info(f"[UserService] New user created: {normalized_email} (id={user_id})")
            return self._to_dict(new_user)

    async def get_user_by_email(self, email: str) -> Optional[dict]:
        from app.db.models import User

        async with self._session_factory() as session:
            result = await session.execute(select(User).where(User.email == email.lower().strip()))
            user = result.scalar_one_or_none()
            return self._to_dict(user) if user else None

    async def get_user_by_id(self, user_id: str) -> Optional[dict]:
        from app.db.models import User

        async with self._session_factory() as session:
            user = await session.get(User, user_id)
            return self._to_dict(user) if user else None

    @staticmethod
    def _to_dict(user) -> dict:
        return {
            "id": user.id,
            "email": user.email,
            "display_name": user.display_name,
            "auth_provider": user.auth_provider,
            "avatar_color": user.avatar_color,
            "created_at": user.created_at.isoformat() if user.created_at else None,
        }
