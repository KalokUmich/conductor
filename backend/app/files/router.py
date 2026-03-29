"""FastAPI router for file upload endpoints."""
import logging
from typing import Optional

from fastapi import APIRouter, File, Form, UploadFile, HTTPException, Request
from fastapi.responses import FileResponse

from .service import FileStorageService
from .schemas import FileUploadResponse, MAX_FILE_SIZE_BYTES

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/files", tags=["files"])


def get_download_url(request: Request, file_id: str) -> str:
    """Generate download URL for a file."""
    base_url = str(request.base_url).rstrip("/")
    return f"{base_url}/files/download/{file_id}"


def _service() -> FileStorageService:
    try:
        return FileStorageService.get_instance()
    except RuntimeError:
        raise HTTPException(status_code=503, detail="File service unavailable (database not connected)")


@router.post("/upload/{room_id}", response_model=FileUploadResponse)
async def upload_file(
    request: Request,
    room_id: str,
    file: UploadFile = File(...),
    user_id: str = Form(...),
    display_name: str = Form(...),
    caption: Optional[str] = Form(None),
):
    """Upload a file to a chat room."""
    try:
        content = await file.read()
        if len(content) > MAX_FILE_SIZE_BYTES:
            raise HTTPException(
                status_code=413,
                detail="File size exceeds limit of 20MB",
            )
        mime_type = file.content_type or "application/octet-stream"
        service = _service()
        metadata = await service.save_file(
            room_id=room_id,
            user_id=user_id,
            display_name=display_name,
            filename=file.filename or "unnamed",
            content=content,
            mime_type=mime_type,
        )
        download_url = get_download_url(request, metadata.id)
        logger.info(
            "File uploaded: %s (%d bytes) to room %s",
            metadata.original_filename, metadata.size_bytes, room_id,
        )
        return FileUploadResponse(
            id=metadata.id,
            original_filename=metadata.original_filename,
            file_type=metadata.file_type,
            mime_type=metadata.mime_type,
            size_bytes=metadata.size_bytes,
            download_url=download_url,
        )
    except ValueError as e:
        raise HTTPException(status_code=413, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error("File upload failed: %s", e)
        raise HTTPException(status_code=500, detail=f"Upload failed: {str(e)}")


@router.get("/download/{file_id}")
async def download_file(file_id: str):
    """Download a file by ID."""
    service = _service()
    metadata = await service.get_file(file_id)
    if not metadata:
        raise HTTPException(status_code=404, detail="File not found")
    file_path = await service.get_file_path(file_id)
    if not file_path:
        raise HTTPException(status_code=404, detail="File not found on disk")
    return FileResponse(
        path=file_path,
        filename=metadata.original_filename,
        media_type=metadata.mime_type,
    )


@router.get("/check-duplicate/{room_id}")
async def check_duplicate(
    request: Request,
    room_id: str,
    filename: str,
):
    """Check if a file with the same name already exists in the room."""
    service = _service()
    room_files = await service.get_room_files(room_id)

    filename_lower = filename.lower()
    match = None
    for f in room_files:
        if f.original_filename.lower() == filename_lower:
            match = f
            break

    if match:
        return {
            "duplicate": True,
            "existing_file": {
                "id": match.id,
                "original_filename": match.original_filename,
                "download_url": get_download_url(request, match.id),
                "uploaded_at": match.uploaded_at,
                "display_name": match.display_name,
                "size_bytes": match.size_bytes,
            },
        }
    return {"duplicate": False, "existing_file": None}


@router.delete("/room/{room_id}")
async def delete_room_files(room_id: str):
    """Delete all files for a room."""
    service = _service()
    count = await service.delete_room_files(room_id)
    logger.info("Deleted %d files for room %s", count, room_id)
    return {"deleted_count": count, "room_id": room_id}
