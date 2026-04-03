"""TODOService — async PostgreSQL-backed room-scoped task tracking."""

import logging
import uuid
from datetime import UTC, datetime
from typing import List, Optional

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from ..db.models import Todo

logger = logging.getLogger(__name__)


class TODOService:
    """Service for managing room-scoped TODOs in PostgreSQL.

    Accepts an ``AsyncEngine`` at construction time instead of managing
    its own database connection.
    """

    _instance: Optional["TODOService"] = None

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._session_factory = async_sessionmaker(engine, expire_on_commit=False)
        logger.info("[TODOService] Initialized with async engine")

    @classmethod
    def get_instance(cls, engine: Optional[AsyncEngine] = None) -> "TODOService":
        if cls._instance is None:
            if engine is None:
                raise RuntimeError("TODOService requires an AsyncEngine on first call")
            cls._instance = cls(engine)
        return cls._instance

    # -----------------------------------------------------------------------
    # CRUD
    # -----------------------------------------------------------------------

    async def create(
        self,
        room_id: str,
        title: str,
        description: Optional[str] = None,
        type_: str = "task",
        priority: str = "medium",
        file_path: Optional[str] = None,
        line_number: Optional[int] = None,
        created_by: str = "",
        assignee: Optional[str] = None,
        source: str = "manual",
        source_id: Optional[str] = None,
    ) -> dict:
        todo_id = str(uuid.uuid4())
        now = datetime.now(UTC)
        todo = Todo(
            id=todo_id,
            room_id=room_id,
            title=title,
            description=description,
            type=type_,
            priority=priority,
            status="open",
            file_path=file_path,
            line_number=line_number,
            created_by=created_by,
            assignee=assignee,
            created_at=now,
            source=source,
            source_id=source_id,
        )
        async with self._session_factory() as session:
            session.add(todo)
            await session.commit()
            await session.refresh(todo)
            return self._row_to_dict(todo)

    async def list_by_room(self, room_id: str) -> List[dict]:
        async with self._session_factory() as session:
            result = await session.execute(select(Todo).where(Todo.room_id == room_id).order_by(Todo.created_at.asc()))
            return [self._row_to_dict(r) for r in result.scalars().all()]

    async def update(self, todo_id: str, **kwargs) -> Optional[dict]:
        allowed = {
            "title",
            "description",
            "priority",
            "status",
            "file_path",
            "line_number",
            "assignee",
        }
        fields = {k: v for k, v in kwargs.items() if k in allowed and v is not None}
        if not fields:
            return await self.get(todo_id)

        async with self._session_factory() as session:
            await session.execute(update(Todo).where(Todo.id == todo_id).values(**fields))
            await session.commit()
        return await self.get(todo_id)

    async def get(self, todo_id: str) -> Optional[dict]:
        async with self._session_factory() as session:
            result = await session.execute(select(Todo).where(Todo.id == todo_id))
            row = result.scalar_one_or_none()
            return self._row_to_dict(row) if row else None

    async def delete(self, todo_id: str) -> bool:
        async with self._session_factory() as session:
            result = await session.execute(delete(Todo).where(Todo.id == todo_id))
            await session.commit()
            return result.rowcount > 0

    async def delete_by_room(self, room_id: str) -> int:
        async with self._session_factory() as session:
            result = await session.execute(delete(Todo).where(Todo.room_id == room_id))
            await session.commit()
            return result.rowcount

    # -----------------------------------------------------------------------
    # Internal
    # -----------------------------------------------------------------------

    @staticmethod
    def _row_to_dict(row: Todo) -> dict:
        return {
            "id": row.id,
            "room_id": row.room_id,
            "title": row.title,
            "description": row.description,
            "type": row.type,
            "priority": row.priority,
            "status": row.status,
            "file_path": row.file_path,
            "line_number": row.line_number,
            "created_by": row.created_by,
            "assignee": row.assignee,
            "created_at": row.created_at.isoformat() if isinstance(row.created_at, datetime) else str(row.created_at),
            "source": row.source,
            "source_id": row.source_id,
        }
