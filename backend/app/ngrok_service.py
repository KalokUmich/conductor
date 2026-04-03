"""Ngrok tunnel service for exposing the backend to the internet.

This module provides functionality to start and manage ngrok tunnels,
allowing external access to the local backend server.

Features:
    - Automatic ngrok tunnel creation on startup using pyngrok
    - Graceful fallback to localhost if ngrok fails
    - Public URL retrieval

Usage:
    from app.ngrok_service import start_ngrok, get_public_url

    # Start ngrok tunnel
    public_url = start_ngrok(port=8000, authtoken="your-token")

    # Get the public URL later
    url = get_public_url()
"""

import logging
from typing import Optional

# Module-level state
_public_url: Optional[str] = None
_tunnel = None

logger = logging.getLogger(__name__)


def start_ngrok(port: int = 8000, authtoken: Optional[str] = None, region: str = "us") -> Optional[str]:
    """Start ngrok tunnel and return the public URL.

    Uses pyngrok library which handles ngrok binary download automatically.

    Args:
        port: Local port to tunnel (default: 8000).
        authtoken: Ngrok authentication token.
        region: Ngrok region (us, eu, ap, au, sa, jp, in).

    Returns:
        The public HTTPS URL if successful, None otherwise.
    """
    global _public_url, _tunnel

    try:
        from pyngrok import conf, ngrok

        # Configure ngrok
        if authtoken:
            conf.get_default().auth_token = authtoken
            logger.info("Ngrok authtoken configured")

        conf.get_default().region = region

        # Start tunnel
        logger.info(f"Starting ngrok tunnel on port {port}...")
        _tunnel = ngrok.connect(port, "http")
        _public_url = _tunnel.public_url

        # Convert http to https if needed
        if _public_url and _public_url.startswith("http://"):
            _public_url = _public_url.replace("http://", "https://")

        logger.info(f"Ngrok tunnel established: {_public_url}")
        return _public_url

    except ImportError:
        logger.error("pyngrok not installed. Install with: pip install pyngrok")
        return None
    except Exception as e:
        logger.error(f"Failed to start ngrok: {e}")
        return None


def get_public_url() -> Optional[str]:
    """Get the cached public URL."""
    return _public_url


def stop_ngrok() -> None:
    """Stop the ngrok tunnel if running."""
    global _tunnel, _public_url

    try:
        from pyngrok import ngrok

        ngrok.kill()
        _tunnel = None
        _public_url = None
        logger.info("Ngrok tunnel stopped")
    except Exception as e:
        logger.warning(f"Error stopping ngrok: {e}")
