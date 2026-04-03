"""TODO tracking router — CRUD endpoints for room-scoped tasks."""

import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from .schemas import TodoCreate, TodoUpdate
from .service import TODOService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/todos", tags=["todos"])


def _service() -> TODOService:
    try:
        return TODOService.get_instance()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="TODO service unavailable (database not connected)") from exc


@router.get("/{room_id}")
async def list_todos(room_id: str) -> JSONResponse:
    """List all TODOs for a room, ordered by creation time."""
    todos = await _service().list_by_room(room_id)
    return JSONResponse(todos)


@router.post("/{room_id}", status_code=201)
async def create_todo(room_id: str, body: TodoCreate) -> JSONResponse:
    """Create a new TODO in a room."""
    todo = await _service().create(
        room_id=room_id,
        title=body.title,
        description=body.description,
        type_=body.type,
        priority=body.priority,
        file_path=body.file_path,
        line_number=body.line_number,
        created_by=body.created_by,
        assignee=body.assignee,
        source=body.source,
        source_id=body.source_id,
    )
    logger.info("[todos] Created %s in room %s: %s", todo["id"], room_id, todo["title"])
    return JSONResponse(todo, status_code=201)


@router.put("/{room_id}/{todo_id}")
async def update_todo(room_id: str, todo_id: str, body: TodoUpdate) -> JSONResponse:
    """Update a TODO's fields."""
    updated = await _service().update(
        todo_id,
        title=body.title,
        description=body.description,
        priority=body.priority,
        status=body.status,
        file_path=body.file_path,
        line_number=body.line_number,
        assignee=body.assignee,
    )
    if updated is None:
        return JSONResponse({"error": "TODO not found"}, status_code=404)
    logger.info("[todos] Updated %s in room %s → status=%s", todo_id, room_id, updated.get("status"))
    return JSONResponse(updated)


@router.delete("/{room_id}/{todo_id}", status_code=204)
async def delete_todo(room_id: str, todo_id: str) -> JSONResponse:
    """Delete a TODO."""
    deleted = await _service().delete(todo_id)
    if not deleted:
        return JSONResponse({"error": "TODO not found"}, status_code=404)
    logger.info("[todos] Deleted %s from room %s", todo_id, room_id)
    return JSONResponse(None, status_code=204)
