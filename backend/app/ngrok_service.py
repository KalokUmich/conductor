"""Ngrok tunnel service for exposing the backend to the internet.

This module provides functionality to start and manage ngrok tunnels,
allowing external access to the local backend server.

Features:
    - Automatic ngrok tunnel creation on startup
    - Graceful fallback to localhost if ngrok fails
    - Public URL retrieval via ngrok API

Usage:
    from app.ngrok_service import start_ngrok, get_public_url
    
    # Start ngrok tunnel (non-blocking)
    public_url = await start_ngrok(port=8000, authtoken="your-token")
    
    # Get the public URL later
    url = get_public_url()
"""
import asyncio
import logging
import subprocess
import sys
from typing import Optional

import httpx

# Module-level state
_ngrok_process: Optional[subprocess.Popen] = None
_public_url: Optional[str] = None

logger = logging.getLogger(__name__)


async def start_ngrok(
    port: int = 8000,
    authtoken: Optional[str] = None,
    region: str = "us"
) -> Optional[str]:
    """Start ngrok tunnel and return the public URL.
    
    Args:
        port: Local port to tunnel (default: 8000).
        authtoken: Ngrok authentication token.
        region: Ngrok region (us, eu, ap, au, sa, jp, in).
    
    Returns:
        The public HTTPS URL if successful, None otherwise.
    """
    global _ngrok_process, _public_url
    
    # Check if ngrok is already running
    existing_url = await _get_ngrok_url_from_api()
    if existing_url:
        _public_url = existing_url
        logger.info(f"Ngrok already running: {_public_url}")
        return _public_url
    
    # Set authtoken if provided
    if authtoken:
        try:
            subprocess.run(
                ["ngrok", "config", "add-authtoken", authtoken],
                check=True,
                capture_output=True
            )
            logger.info("Ngrok authtoken configured")
        except subprocess.CalledProcessError as e:
            logger.warning(f"Failed to set ngrok authtoken: {e}")
        except FileNotFoundError:
            logger.error("Ngrok not found. Please install ngrok first.")
            return None
    
    # Start ngrok process
    try:
        cmd = ["ngrok", "http", str(port), "--region", region]
        _ngrok_process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        logger.info(f"Starting ngrok tunnel on port {port}...")
        
        # Wait for ngrok to start and get the URL
        for attempt in range(10):
            await asyncio.sleep(1)
            url = await _get_ngrok_url_from_api()
            if url:
                _public_url = url
                logger.info(f"Ngrok tunnel established: {_public_url}")
                return _public_url
        
        logger.error("Ngrok started but could not get public URL")
        return None
        
    except FileNotFoundError:
        logger.error(
            "Ngrok not found. Please install ngrok: "
            "https://ngrok.com/download"
        )
        return None
    except Exception as e:
        logger.error(f"Failed to start ngrok: {e}")
        return None


async def _get_ngrok_url_from_api() -> Optional[str]:
    """Get the public URL from ngrok's local API."""
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                "http://localhost:4040/api/tunnels",
                timeout=2.0
            )
            if response.status_code == 200:
                data = response.json()
                tunnels = data.get("tunnels", [])
                # Find HTTPS tunnel
                for tunnel in tunnels:
                    if tunnel.get("proto") == "https":
                        return tunnel.get("public_url")
                # Fallback to any tunnel
                if tunnels:
                    return tunnels[0].get("public_url")
    except Exception:
        pass
    return None


def get_public_url() -> Optional[str]:
    """Get the cached public URL."""
    return _public_url


def stop_ngrok() -> None:
    """Stop the ngrok process if running."""
    global _ngrok_process, _public_url
    if _ngrok_process:
        _ngrok_process.terminate()
        _ngrok_process = None
        _public_url = None
        logger.info("Ngrok tunnel stopped")

