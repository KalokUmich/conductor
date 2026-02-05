"""Conductor Backend Application.

This is the main entry point for the Conductor backend service.
Conductor is a VS Code extension that combines Live Share, real-time chat,
and AI-powered code generation for collaborative development.

Modules:
    - chat: WebSocket-based real-time chat rooms
    - summary: Chat message summarization (keyword-based extraction)
    - agent: AI code generation (currently MockAgent for testing)
    - policy: Auto-apply policy evaluation
    - audit: DuckDB-based audit logging
"""
from fastapi import FastAPI

from app.agent.router import router as agent_router
from app.audit.router import router as audit_router
from app.chat.router import router as chat_router
from app.policy.router import router as policy_router
from app.summary.router import router as summary_router

# Create FastAPI application with metadata
app = FastAPI(
    title="Conductor API",
    description="Backend service for Conductor - AI collaborative coding extension",
    version="0.1.0",
)

# Register all routers
app.include_router(chat_router)
app.include_router(summary_router)
app.include_router(agent_router)
app.include_router(policy_router)
app.include_router(audit_router)


@app.get("/health")
async def health() -> dict:
    """Health check endpoint.

    Returns:
        dict: Status object indicating the server is running.
    """
    return {"status": "ok"}

