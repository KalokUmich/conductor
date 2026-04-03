"""File upload and storage module for Conductor.

This module handles file uploads, storage, and cleanup for chat sessions.
Files are stored locally in a temporary directory and metadata is tracked in PostgreSQL.

Supported file types:
- Images: jpg, jpeg, png, gif, webp, svg
- Documents: pdf
- Audio: mp3, wav, ogg, m4a, flac
- Any file under 20MB

When a session ends, all files for that room are deleted.
TODO: Consider backing up to cloud storage before deletion.
"""
