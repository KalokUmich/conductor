"""File storage service for Conductor.

Handles file storage on disk and metadata tracking in PostgreSQL (async SQLAlchemy).
Files are stored in: uploads/{room_id}/{uuid}.{ext}
"""

import logging
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import List, Optional

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from ..db.models import FileMetadataRecord
from .schemas import MAX_FILE_SIZE_BYTES, FileMetadata, FileType, get_file_type

logger = logging.getLogger(__name__)


class FileStorageService:
    """Service for managing file uploads and storage."""

    _instance: Optional["FileStorageService"] = None
    _upload_dir: str = "uploads"

    def __init__(self, engine: AsyncEngine, upload_dir: Optional[str] = None) -> None:
        self._engine = engine
        self._session_factory = async_sessionmaker(engine, expire_on_commit=False)
        if upload_dir:
            self._upload_dir = upload_dir
        self._ensure_upload_dir()

    @classmethod
    def get_instance(
        cls,
        engine: Optional[AsyncEngine] = None,
        upload_dir: Optional[str] = None,
    ) -> "FileStorageService":
        if cls._instance is None:
            if engine is None:
                raise RuntimeError("FileStorageService requires an AsyncEngine on first call")
            cls._instance = cls(engine, upload_dir)
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        cls._instance = None

    def _ensure_upload_dir(self) -> None:
        Path(self._upload_dir).mkdir(parents=True, exist_ok=True)

    def _get_room_dir(self, room_id: str) -> Path:
        return Path(self._upload_dir) / room_id

    async def save_file(
        self,
        room_id: str,
        user_id: str,
        display_name: str,
        filename: str,
        content: bytes,
        mime_type: str,
    ) -> FileMetadata:
        """Save an uploaded file to disk and record metadata."""
        size_bytes = len(content)
        if size_bytes > MAX_FILE_SIZE_BYTES:
            raise ValueError(f"File size ({size_bytes} bytes) exceeds limit ({MAX_FILE_SIZE_BYTES} bytes = 20MB)")

        file_id = str(uuid.uuid4())
        ext = Path(filename).suffix.lower() or ""
        stored_filename = f"{file_id}{ext}"
        file_type = get_file_type(mime_type)

        room_dir = self._get_room_dir(room_id)
        room_dir.mkdir(parents=True, exist_ok=True)

        file_path = room_dir / stored_filename
        file_path.write_bytes(content)
        logger.info("Saved file: %s (%d bytes)", file_path, size_bytes)

        now = datetime.now(UTC)
        metadata = FileMetadata(
            id=file_id,
            room_id=room_id,
            user_id=user_id,
            display_name=display_name,
            original_filename=filename,
            stored_filename=stored_filename,
            file_type=file_type,
            mime_type=mime_type,
            size_bytes=size_bytes,
        )

        row = FileMetadataRecord(
            id=file_id,
            room_id=room_id,
            user_id=user_id,
            display_name=display_name,
            original_filename=filename,
            stored_filename=stored_filename,
            file_type=file_type.value,
            mime_type=mime_type,
            size_bytes=size_bytes,
            uploaded_at=now,
        )
        async with self._session_factory() as session:
            session.add(row)
            await session.commit()

        return metadata

    async def get_file(self, file_id: str) -> Optional[FileMetadata]:
        """Get file metadata by ID."""
        async with self._session_factory() as session:
            result = await session.execute(select(FileMetadataRecord).where(FileMetadataRecord.id == file_id))
            row = result.scalar_one_or_none()
            if not row:
                return None
            return self._row_to_metadata(row)

    async def get_file_path(self, file_id: str) -> Optional[Path]:
        """Get the file path on disk for a file ID."""
        metadata = await self.get_file(file_id)
        if not metadata:
            return None
        file_path = self._get_room_dir(metadata.room_id) / metadata.stored_filename
        if not file_path.exists():
            return None
        return file_path

    async def get_room_files(self, room_id: str) -> List[FileMetadata]:
        """Get all files for a room."""
        async with self._session_factory() as session:
            result = await session.execute(
                select(FileMetadataRecord)
                .where(FileMetadataRecord.room_id == room_id)
                .order_by(FileMetadataRecord.uploaded_at.asc())
            )
            return [self._row_to_metadata(r) for r in result.scalars().all()]

    async def delete_room_files(self, room_id: str) -> int:
        """Delete all files for a room."""
        files = await self.get_room_files(room_id)
        file_count = len(files)
        if file_count == 0:
            return 0

        logger.info("Deleting %d files for room %s", file_count, room_id)

        room_dir = self._get_room_dir(room_id)
        if room_dir.exists():
            shutil.rmtree(room_dir)
            logger.info("Deleted directory: %s", room_dir)

        async with self._session_factory() as session:
            await session.execute(delete(FileMetadataRecord).where(FileMetadataRecord.room_id == room_id))
            await session.commit()

        logger.info("Deleted %d file records for room %s", file_count, room_id)
        return file_count

    @staticmethod
    def _row_to_metadata(row: FileMetadataRecord) -> FileMetadata:
        return FileMetadata(
            id=row.id,
            room_id=row.room_id,
            user_id=row.user_id,
            display_name=row.display_name,
            original_filename=row.original_filename,
            stored_filename=row.stored_filename,
            file_type=FileType(row.file_type),
            mime_type=row.mime_type,
            size_bytes=row.size_bytes,
            uploaded_at=row.uploaded_at.timestamp() if row.uploaded_at else 0,
        )
